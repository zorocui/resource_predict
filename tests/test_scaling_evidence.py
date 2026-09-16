from dataclasses import replace
from unittest.mock import patch, Mock
from types import SimpleNamespace

import pandas as pd
import pytest

from resource_predict.providers import k8s_prometheus as provider
from tests.test_k8s_workload_provider import BASE_TS, GIB, FakePrometheusClient, _target


class HistoricalClient(FakePrometheusClient):
    def query_range(self, query, *, start, end, step):
        rows = super().query_range(query, start=start, end=end, step=step)
        if "kube_pod_container_resource_" in query:
            divisor = GIB if 'resource="memory"' in query else 1
            return [self._range_row("ns", "api-rs-a", "app", "", [4 * divisor, 2 * divisor])]
        return rows


def test_fetch_collects_historical_capacity_independent_of_latest_spec():
    HistoricalClient.range_calls = []
    target = replace(_target("cluster-a"), rate_window="7m")
    with patch.object(provider, "PrometheusClient", HistoricalClient), patch.object(provider, "time", SimpleNamespace(time=Mock(side_effect=[BASE_TS + 600, BASE_TS + 900]))):
        item, = provider._fetch_target(target, 0)
    evidence = item["scaling_evidence"]
    assert evidence["source"] == "k8s_prometheus_scaling_unfilled"
    assert evidence["collected_at_ms"] == (BASE_TS + 900) * 1000
    assert evidence["sample_interval_ms"] == 300_000
    assert evidence["spec"]["containers"]["app"]["cpu_request_cores"] == 1
    cpu = next(row for row in evidence["series"] if row["container"] == "app" and row["metric"] == "cpu" and row["basis"] == "request")
    assert cpu["capacity"] == [4, 2]
    assert cpu["usage"] == [0.2, 0.4]
    assert cpu["timestamps"] == [BASE_TS * 1000, (BASE_TS + 300) * 1000]
    memory = next(row for row in evidence["series"] if row["container"] == "app" and row["metric"] == "memory")
    assert memory["usage"] == [0.5, 0.6]
    assert memory["capacity"] == [4, 2]
    assert memory["unit"] == "GiB"
    assert evidence["provenance"]["owner_mapping"] == "historical_range_unique_owner_with_current_fallback"
    assert evidence["limitations"]
    assert len(HistoricalClient.range_calls) == 10
    assert "[7m]" in HistoricalClient.range_calls[0]["query"]


def test_missing_configured_capacity_stops_fetch_without_fabricating_history():
    class MissingClient(FakePrometheusClient):
        def query_range(self, query, **kwargs):
            if "kube_pod_container_resource_limits" in query:
                raise TimeoutError("capacity unavailable")
            return super().query_range(query, **kwargs)

    with patch.object(provider, "PrometheusClient", MissingClient):
        with pytest.raises(RuntimeError, match="历史容量查询无结果"):
            provider._fetch_target(_target("cluster-a"), 0)
    history, errors = provider._scaling_capacity_history(MissingClient(), "", 0, 1000, 300)
    assert any("TimeoutError" in error for error in errors)
    assert history[("cpu", "limit")] == {}
    assert history[("memory", "limit")] == {}


def test_evidence_matches_members_and_timestamps_without_filling_gaps():
    first = ("ns", "a", "app")
    second = ("ns", "b", "app")
    owner = {("ns", "a"): ("Deployment", "api"), ("ns", "b"): ("Deployment", "api")}

    def series(values, timestamps=(0, 300, 600)):
        return pd.Series(values, index=pd.to_datetime(timestamps, unit="s", utc=True))

    usage = {first: series([1, 2, 3]), second: series([2, 4, 5])}
    capacity = {first: series([4, 4, 4]), second: series([8, 8], (0, 600))}
    result = provider._scaling_series_by_workload(usage, {}, {("cpu", "request"): capacity}, owner)
    row = next(row for row in result[("ns", "Deployment", "api")] if row["metric"] == "cpu" and row["basis"] == "request")
    assert row["usage"] == [3, None, 8]
    assert row["capacity"] == [12, None, 12]
    # Usage missing at a point where capacity establishes membership also invalidates both totals.
    usage[second] = series([2, 5], (0, 600))
    capacity[second] = series([8, 8, 8])
    result = provider._scaling_series_by_workload(usage, {}, {("cpu", "request"): capacity}, owner)
    row = result[("ns", "Deployment", "api")][0]
    assert row["usage"] == [3, None, 8]
    assert row["capacity"] == [12, None, 12]
    # A one-second offset is not a matching timestamp.
    capacity[first] = series([4, 4, 4], (1, 301, 601))
    result = provider._scaling_series_by_workload(usage, {}, {("cpu", "request"): capacity}, owner)
    assert all(value is None for value in result[("ns", "Deployment", "api")][0]["capacity"])


def test_changing_observed_replica_set_and_invalid_samples():
    first, second = ("ns", "a", "app"), ("ns", "b", "app")
    owner = {("ns", "a"): ("Deployment", "api"), ("ns", "b"): ("Deployment", "api")}
    index = pd.to_datetime([0, 300, 600, 900], unit="s", utc=True)
    usage = {first: pd.Series([0, 1, float("nan"), float("inf")], index=index),
             second: pd.Series([2], index=index[:1])}
    capacity = {first: pd.Series([4, 2, 2, 2], index=index),
                second: pd.Series([4], index=index[:1])}
    result = provider._scaling_series_by_workload(usage, {}, {("cpu", "request"): capacity}, owner)
    row = result[("ns", "Deployment", "api")][0]
    assert row["usage"] == [2, 1, None, None]
    assert row["capacity"] == [8, 2, None, None]
