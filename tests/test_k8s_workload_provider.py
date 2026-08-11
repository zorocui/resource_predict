from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from resource_predict.providers import k8s_prometheus as provider
from resource_predict.providers.k8s_prometheus import PrometheusTarget


BASE_TS = 1_700_000_000
GIB = 1024 ** 3


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self._body


def _target(cluster: str) -> PrometheusTarget:
    return PrometheusTarget(
        cluster=cluster,
        prometheus_url=f"http://{cluster}.example",
        namespace_regex="",
        bearer_token="",
        basic_auth="",
        history_days=1,
        step_seconds=300,
        request_timeout_seconds=5,
    )


class FakePrometheusClient:
    queries: list[str] = []
    range_calls: list[dict] = []
    init_kwargs: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs.append(dict(kwargs))

    def query_range(self, query: str, *, start: float, end: float, step: int):
        self.queries.append(query)
        self.range_calls.append({"query": query, "start": start, "end": end, "step": step})
        if "container_cpu_usage_seconds_total" in query:
            rows = [
                self._range_row("ns", "api-rs-a", "app", "node-1", [0.2, 0.4]),
                self._range_row("ns", "api-rs-b", "sidecar", "node-2", [0.1, 0.2]),
                self._range_row("ns", "orphan", "app", "node-3", [9.0, 9.0]),
            ]
            if self._includes_pod_container(query):
                rows.append(self._range_row("ns", "api-rs-a", "POD", "node-1", [5.0, 5.0]))
            return rows
        if "container_memory_working_set_bytes" in query:
            rows = [
                self._range_row("ns", "api-rs-a", "app", "node-1", [0.5 * GIB, 0.6 * GIB]),
                self._range_row("ns", "api-rs-b", "sidecar", "node-2", [0.5 * GIB, 0.8 * GIB]),
                self._range_row("ns", "orphan", "app", "node-3", [9.0 * GIB, 9.0 * GIB]),
            ]
            if self._includes_pod_container(query):
                rows.append(self._range_row("ns", "api-rs-a", "POD", "node-1", [5.0 * GIB, 5.0 * GIB]))
            return rows
        return []

    def query(self, query: str, *, ts=None):
        self.queries.append(query)
        if "kube_pod_owner" in query:
            return [
                self._instant_row(
                    {"namespace": "ns", "pod": "api-rs-a", "owner_kind": "ReplicaSet", "owner_name": "api-rs"},
                    1,
                ),
                self._instant_row(
                    {"namespace": "ns", "pod": "api-rs-b", "owner_kind": "ReplicaSet", "owner_name": "api-rs"},
                    1,
                ),
            ]
        if "kube_replicaset_owner" in query:
            return [
                self._instant_row(
                    {
                        "namespace": "ns",
                        "replicaset": "api-rs",
                        "owner_kind": "Deployment",
                        "owner_name": "api",
                    },
                    1,
                )
            ]
        if "kube_deployment_spec_replicas" in query:
            return [
                self._instant_row({"namespace": "ns", "deployment": "api"}, 3),
            ]
        if (
            "kube_deployment_status_replicas" in query
            or "kube_statefulset_replicas" in query
            or "kube_statefulset_status_replicas" in query
            or "kube_daemonset_status_desired_number_scheduled" in query
        ):
            return []
        if "requests_cpu_cores" in query or 'resource="cpu"' in query and "requests" in query:
            rows = [
                self._resource_row("ns", "api-rs-a", "app", 1.0),
                self._resource_row("ns", "api-rs-b", "sidecar", 1.0),
                self._resource_row("ns", "orphan", "app", 10.0),
            ]
            if self._includes_pod_container(query):
                rows.append(self._resource_row("ns", "api-rs-a", "POD", 0.5))
            return rows
        if "limits_cpu_cores" in query or 'resource="cpu"' in query and "limits" in query:
            return [self._resource_row("ns", "api-rs-a", "POD", 0.5)] if self._includes_pod_container(query) else []
        if "requests_memory_bytes" in query or 'resource="memory"' in query and "requests" in query:
            return [self._resource_row("ns", "api-rs-a", "POD", 0.5 * GIB)] if self._includes_pod_container(query) else []
        if "limits_memory_bytes" in query or 'resource="memory"' in query and "limits" in query:
            rows = [
                self._resource_row("ns", "api-rs-a", "app", 1.0 * GIB),
                self._resource_row("ns", "api-rs-b", "sidecar", 1.0 * GIB),
                self._resource_row("ns", "orphan", "app", 10.0 * GIB),
            ]
            if self._includes_pod_container(query):
                rows.append(self._resource_row("ns", "api-rs-a", "POD", 0.5 * GIB))
            return rows
        return []

    @staticmethod
    def _range_row(namespace: str, pod: str, container: str, node: str, values: list[float]) -> dict:
        return {
            "metric": {"namespace": namespace, "pod": pod, "container": container, "node": node},
            "values": [[BASE_TS + i * 300, str(value)] for i, value in enumerate(values)],
        }

    @staticmethod
    def _resource_row(namespace: str, pod: str, container: str, value: float) -> dict:
        return FakePrometheusClient._instant_row(
            {"namespace": namespace, "pod": pod, "container": container},
            value,
        )

    @staticmethod
    def _instant_row(metric: dict, value: float) -> dict:
        return {"metric": metric, "value": [BASE_TS, str(value)]}

    @staticmethod
    def _includes_pod_container(query: str) -> bool:
        return 'container!="POD"' not in query


class AsymmetricResourcePrometheusClient:
    def __init__(self, *args, **kwargs):
        pass

    def query_range(self, query: str, *, start: float, end: float, step: int):
        if "container_cpu_usage_seconds_total" in query:
            return [
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "alertmanager", "node-1", [0.01, 0.02]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "config-reloader", "node-1", [0.01, 0.02]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "alertmanager", "node-2", [0.01, 0.02]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "config-reloader", "node-2", [0.01, 0.02]),
            ]
        if "container_memory_working_set_bytes" in query:
            return [
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "alertmanager", "node-1", [10 * 1024 ** 2, 20 * 1024 ** 2]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "config-reloader", "node-1", [1 * 1024 ** 2, 2 * 1024 ** 2]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "alertmanager", "node-2", [10 * 1024 ** 2, 20 * 1024 ** 2]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "config-reloader", "node-2", [1 * 1024 ** 2, 2 * 1024 ** 2]),
            ]
        return []

    def query(self, query: str, *, ts=None):
        if "kube_pod_owner" in query:
            return [
                FakePrometheusClient._instant_row(
                    {
                        "namespace": "monitoring",
                        "pod": "alertmanager-main-0",
                        "owner_kind": "StatefulSet",
                        "owner_name": "alertmanager-main",
                    },
                    1,
                ),
                FakePrometheusClient._instant_row(
                    {
                        "namespace": "monitoring",
                        "pod": "alertmanager-main-1",
                        "owner_kind": "StatefulSet",
                        "owner_name": "alertmanager-main",
                    },
                    1,
                ),
            ]
        if "kube_statefulset_replicas" in query:
            return [FakePrometheusClient._instant_row({"namespace": "monitoring", "statefulset": "alertmanager-main"}, 2)]
        if "requests_cpu_cores" in query or 'resource="cpu"' in query and "requests" in query:
            return []
        if "limits_cpu_cores" in query or 'resource="cpu"' in query and "limits" in query:
            return [
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-0", "config-reloader", 0.1),
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-1", "config-reloader", 0.1),
            ]
        if "requests_memory_bytes" in query or 'resource="memory"' in query and "requests" in query:
            return [
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-0", "alertmanager", 200 * 1024 ** 2),
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-1", "alertmanager", 200 * 1024 ** 2),
            ]
        if "limits_memory_bytes" in query or 'resource="memory"' in query and "limits" in query:
            return [
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-0", "config-reloader", 25 * 1024 ** 2),
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-1", "config-reloader", 25 * 1024 ** 2),
            ]
        return []


class K8SWorkloadProviderTest(unittest.TestCase):
    def setUp(self):
        FakePrometheusClient.queries = []
        FakePrometheusClient.range_calls = []
        FakePrometheusClient.init_kwargs = []

    def test_prometheus_client_retries_503_then_succeeds(self):
        error = urllib.error.HTTPError("http://prom", 503, "busy", {}, None)
        response = _FakeResponse({"status": "success", "data": {"result": []}})
        client = provider.PrometheusClient(
            "http://prom", max_attempts=3, retry_backoff_seconds=0.25
        )

        with patch("urllib.request.urlopen", side_effect=[error, response]) as open_url:
            with patch("resource_predict.providers.k8s_prometheus.time.sleep") as sleep:
                self.assertEqual(client.query("up"), [])

        self.assertEqual(open_url.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_prometheus_client_does_not_retry_bad_request(self):
        error = urllib.error.HTTPError("http://prom", 400, "bad query", {}, None)
        client = provider.PrometheusClient("http://prom", max_attempts=3)

        with patch("urllib.request.urlopen", side_effect=error) as open_url:
            with self.assertRaises(urllib.error.HTTPError):
                client.query("bad")

        self.assertEqual(open_url.call_count, 1)

    def test_prometheus_client_retries_transient_failures_with_exponential_backoff(self):
        transient_errors = (
            urllib.error.URLError("network unavailable"),
            TimeoutError("timed out"),
            urllib.error.HTTPError("http://prom", 429, "rate limited", {}, None),
            urllib.error.HTTPError("http://prom", 500, "server error", {}, None),
        )
        response = _FakeResponse({"status": "success", "data": {"result": []}})

        for error in transient_errors:
            with self.subTest(error=type(error).__name__, status=getattr(error, "code", None)):
                client = provider.PrometheusClient(
                    "http://prom",
                    bearer_token="top-secret-token",
                    max_attempts=3,
                    retry_backoff_seconds=0.5,
                )
                with patch(
                    "urllib.request.urlopen",
                    side_effect=[error, error, response],
                ) as open_url:
                    with patch("resource_predict.providers.k8s_prometheus.time.sleep") as sleep:
                        with patch.object(provider.logger, "warning") as warning:
                            self.assertEqual(client.query("up"), [])

                self.assertEqual(open_url.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])
                self.assertNotIn("top-secret-token", repr(warning.call_args_list))

    def test_prometheus_client_does_not_retry_semantic_failure(self):
        response = _FakeResponse({"status": "error", "error": "invalid expression"})
        client = provider.PrometheusClient("http://prom", max_attempts=3)

        with patch("urllib.request.urlopen", return_value=response) as open_url:
            with self.assertRaisesRegex(RuntimeError, "Prometheus query failed"):
                client.query("bad")

        self.assertEqual(open_url.call_count, 1)

    def test_query_range_chunks_and_merges_by_full_metric_labels(self):
        client = provider.PrometheusClient(
            "http://prom", range_query_chunk_hours=24
        )
        chunks = [
            {"result": [
                {"metric": {"pod": "a", "container": "app"}, "values": [[10, "1"], [20, "2"]]},
                {"metric": {"pod": "a", "container": "sidecar"}, "values": [[10, "8"]]},
            ]},
            {"result": [
                {"metric": {"container": "app", "pod": "a"}, "values": [[20, "3"], [30, "4"]]},
            ]},
            {"result": [
                {"metric": {"pod": "a", "container": "app"}, "values": [[40, "5"]]},
            ]},
        ]

        with patch.object(provider.PrometheusClient, "_get", side_effect=chunks) as get:
            rows = client.query_range("metric", start=0, end=49 * 3600, step=10)

        self.assertEqual(get.call_count, 3)
        params = [call.args[1] for call in get.call_args_list]
        self.assertEqual(
            [(item["start"], item["end"]) for item in params],
            [(0, 24 * 3600), (24 * 3600, 48 * 3600), (48 * 3600, 49 * 3600)],
        )
        app_row = next(row for row in rows if row["metric"]["container"] == "app")
        self.assertEqual(app_row["values"], [[10, "1"], [20, "3"], [30, "4"], [40, "5"]])
        self.assertEqual(len(rows), 2)

    def test_query_range_returns_no_partial_rows_when_later_chunk_exhausts_retries(self):
        first = _FakeResponse({
            "status": "success",
            "data": {"result": [{"metric": {"pod": "a"}, "values": [[10, "1"]]}]},
        })
        failure = urllib.error.URLError("network unavailable")
        client = provider.PrometheusClient(
            "http://prom",
            max_attempts=3,
            retry_backoff_seconds=0.25,
            range_query_chunk_hours=24,
        )

        with patch("urllib.request.urlopen", side_effect=[first, failure, failure, failure]) as open_url:
            with patch("resource_predict.providers.k8s_prometheus.time.sleep") as sleep:
                with self.assertRaises(urllib.error.URLError):
                    client.query_range("metric", start=0, end=25 * 3600, step=10)

        self.assertEqual(open_url.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5])

    def test_structured_fetch_reports_all_clusters_successful(self):
        targets = [_target("cluster-a"), _target("cluster-b")]

        def fake_fetch(target, limit, *, history_hours=None):
            return [{"resource_id": f"k8s:{target.cluster}:ns:deployment:api"}]

        with patch.object(provider, "_resolve_targets", return_value=targets):
            with patch.object(provider, "_fetch_target", side_effect=fake_fetch):
                with patch.object(provider, "settings", SimpleNamespace(
                    k8s_prometheus=SimpleNamespace(fail_fast=False)
                )):
                    result = provider.fetch_k8s_workload_prometheus_result(
                        resources=0, n=0, freq="5min"
                    )

        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(
            [(row["cluster"], row["status"]) for row in result["cluster_results"]],
            [("cluster-a", "success"), ("cluster-b", "success")],
        )

    def test_structured_fetch_reports_partial_success(self):
        targets = [_target("cluster-a"), _target("cluster-b")]

        def fake_fetch(target, limit, *, history_hours=None):
            if target.cluster == "cluster-b":
                raise TimeoutError("Prometheus timeout")
            return [{"resource_id": "k8s:cluster-a:ns:deployment:api"}]

        with patch.object(provider, "_resolve_targets", return_value=targets):
            with patch.object(provider, "_fetch_target", side_effect=fake_fetch):
                with patch.object(provider, "settings", SimpleNamespace(
                    k8s_prometheus=SimpleNamespace(fail_fast=False)
                )):
                    result = provider.fetch_k8s_workload_prometheus_result(
                        resources=0, n=0, freq="5min"
                    )

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(
            [(row["cluster"], row["status"]) for row in result["cluster_results"]],
            [("cluster-a", "success"), ("cluster-b", "failed")],
        )
        self.assertIn("Prometheus timeout", result["cluster_results"][1]["error"])

    def test_structured_fetch_marks_empty_and_missing_clusters_failed(self):
        with patch.object(provider, "_resolve_targets", return_value=[_target("cluster-a")]):
            with patch.object(provider, "_fetch_target", return_value=[]):
                with patch.object(provider, "settings", SimpleNamespace(
                    k8s_prometheus=SimpleNamespace(fail_fast=False)
                )):
                    result = provider.fetch_k8s_workload_prometheus_result(
                        resources=0,
                        n=0,
                        freq="5min",
                        clusters=["cluster-a", "cluster-missing"],
                    )

        by_cluster = {row["cluster"]: row for row in result["cluster_results"]}
        self.assertIn("未返回可聚合", by_cluster["cluster-a"]["error"])
        self.assertIn("未找到 K8S Prometheus 集群配置", by_cluster["cluster-missing"]["error"])

    def test_structured_fetch_records_fail_fast_targets_without_calling_them(self):
        calls = []

        def fake_fetch(target, limit, *, history_hours=None):
            calls.append(target.cluster)
            raise RuntimeError("boom")

        with patch.object(provider, "_resolve_targets", return_value=[_target("cluster-a"), _target("cluster-b")]):
            with patch.object(provider, "_fetch_target", side_effect=fake_fetch):
                with patch.object(provider, "settings", SimpleNamespace(
                    k8s_prometheus=SimpleNamespace(fail_fast=True)
                )):
                    result = provider.fetch_k8s_workload_prometheus_result(
                        resources=0, n=0, freq="5min"
                    )

        self.assertEqual(calls, ["cluster-a"])
        self.assertEqual([row["status"] for row in result["cluster_results"]], ["failed", "failed"])
        self.assertIn("fail_fast", result["cluster_results"][1]["error"])

    def test_list_provider_remains_compatible(self):
        structured = {
            "items": [{"resource_id": "k8s:cluster-a:ns:deployment:api"}],
            "cluster_results": [],
        }
        with patch.object(provider, "fetch_k8s_workload_prometheus_result", return_value=structured):
            items = provider.k8s_workload_prometheus_provider(resources=0, n=0, freq="5min")
        self.assertEqual(items, structured["items"])

    def test_resolve_targets_prefers_file_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k8s_prometheus_clusters.json"
            path.write_text(
                json.dumps([{"cluster": "file-cluster", "prometheus_url": "http://file-prometheus:9090"}]),
                encoding="utf-8",
            )
            with patch.object(provider, "K8S_PROMETHEUS_CONFIG_PATH", path):
                with patch.dict("os.environ", {"K8S_PROMETHEUS_CLUSTERS": '{"env-cluster":"http://env:9090"}'}):
                    targets = provider._resolve_targets()

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].cluster, "file-cluster")
        self.assertEqual(targets[0].prometheus_url, "http://file-prometheus:9090")

    def test_fetch_target_aggregates_pods_to_deployment_workload(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            items = provider._fetch_target(target, limit=0)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["resource_id"], "k8s:cluster-a:ns:deployment:api")
        self.assertEqual(item["resource_type"], "k8s_workload")
        self.assertEqual(item["spec"]["workload_kind"], "Deployment")
        self.assertEqual(item["spec"]["workload_name"], "api")
        self.assertEqual(item["spec"]["pods_observed"], ["api-rs-a", "api-rs-b"])
        self.assertEqual(item["spec"]["containers_observed"], ["app", "sidecar"])
        self.assertEqual(item["spec"]["replicas"], 3)
        self.assertEqual(item["spec"]["replicas_observed"], 3)
        self.assertEqual(item["spec"]["nodes"], ["node-1", "node-2"])
        self.assertNotIn("cpu_request_cores", item["spec"])
        self.assertNotIn("memory_limit_gb", item["spec"])
        self.assertNotIn("cpu_request_cores_total", item["spec"])
        self.assertNotIn("memory_limit_gb_total", item["spec"])
        self.assertEqual(item["spec"]["cpu_limit_metric_mode"], "cpu_usage_cores")
        self.assertEqual(item["spec"]["cpu_request_metric_mode"], "cpu_usage/cpu_request")
        self.assertEqual(item["spec"]["memory_limit_metric_mode"], "memory_working_set/memory_limit")
        self.assertEqual(item["spec"]["memory_request_metric_mode"], "memory_working_set_gb")
        self.assertEqual(
            item["spec"]["containers"],
            {
                "app": {
                    "cpu_request_cores": 1.0,
                    "cpu_limit_cores": None,
                    "memory_request_gb": None,
                    "memory_limit_gb": 1.0,
                },
                "sidecar": {
                    "cpu_request_cores": 1.0,
                    "cpu_limit_cores": None,
                    "memory_request_gb": None,
                    "memory_limit_gb": 1.0,
                },
            },
        )
        self.assertTrue(any('container!="POD"' in query for query in FakePrometheusClient.queries))
        self.assertEqual(set(item["metrics"]), {"cpu_limit", "cpu_request", "memory_limit", "memory_request"})
        self.assertEqual(set(item["container_metrics"]), {"app", "sidecar"})
        self.assertEqual(
            set(item["container_metrics"]["app"]),
            {"cpu_limit", "cpu_request", "memory_limit", "memory_request"},
        )
        self.assertIn("app", item["container_data_quality"])
        self.assertEqual(item["container_metric_modes"]["app"]["cpu_request"], "cpu_usage/cpu_request")
        self.assertAlmostEqual(item["metrics"]["cpu_limit"]["values"][0], 0.3)
        self.assertAlmostEqual(item["metrics"]["cpu_limit"]["values"][1], 0.6)
        self.assertAlmostEqual(item["metrics"]["cpu_request"]["values"][0], 0.15)
        self.assertAlmostEqual(item["metrics"]["cpu_request"]["values"][1], 0.3)
        self.assertAlmostEqual(item["metrics"]["memory_limit"]["values"][0], 0.5)
        self.assertAlmostEqual(item["metrics"]["memory_limit"]["values"][1], 0.7)
        self.assertAlmostEqual(item["metrics"]["memory_request"]["values"][0], 1.0)
        self.assertAlmostEqual(item["metrics"]["memory_request"]["values"][1], 1.4)

    def test_diagnose_target_reports_workload_readiness(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            report = provider._diagnose_target(target)

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["workloads_resolved"], 1)
        self.assertEqual(report["counts"]["workload_replica_rows"], 1)
        self.assertEqual(report["counts"]["orphan_container_series"], 1)
        self.assertEqual(report["sample_workloads"], [
            {"namespace": "ns", "workload_kind": "Deployment", "workload_name": "api"}
        ])
        self.assertTrue(any("缺少 owner" in warning for warning in report["warnings"]))
        self.assertEqual(report["errors"], [])
        self.assertEqual(
            FakePrometheusClient.init_kwargs[0]["max_attempts"],
            provider.settings.k8s_prometheus.request_max_attempts,
        )
        self.assertEqual(
            FakePrometheusClient.init_kwargs[0]["retry_backoff_seconds"],
            provider.settings.k8s_prometheus.retry_backoff_seconds,
        )
        self.assertEqual(
            FakePrometheusClient.init_kwargs[0]["range_query_chunk_hours"],
            provider.settings.k8s_prometheus.range_query_chunk_hours,
        )

    def test_replicaset_owner_query_does_not_include_pod_selector(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="prod|default",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            provider._diagnose_target(target)

        queries = [q for q in FakePrometheusClient.queries if "kube_replicaset_owner" in q]
        self.assertTrue(queries)
        self.assertTrue(all('pod!=""' not in query for query in queries))
        self.assertTrue(any('namespace=~"prod|default"' in query for query in queries))

    def test_cpu_usage_query_uses_ten_minute_rate_window(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
            rate_window="10m",
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            provider._diagnose_target(target)

        cpu_queries = [q for q in FakePrometheusClient.queries if "container_cpu_usage_seconds_total" in q]
        self.assertTrue(cpu_queries)
        self.assertTrue(all("[10m]" in query for query in cpu_queries))

    def test_fetch_target_uses_configured_cpu_rate_window(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
            rate_window="7m",
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            provider._fetch_target(target, limit=0)

        cpu_queries = [q for q in FakePrometheusClient.queries if "container_cpu_usage_seconds_total" in q]
        self.assertTrue(cpu_queries)
        self.assertTrue(all("[7m]" in query for query in cpu_queries))
        self.assertEqual(
            FakePrometheusClient.init_kwargs[0]["max_attempts"],
            provider.settings.k8s_prometheus.request_max_attempts,
        )
        self.assertEqual(
            FakePrometheusClient.init_kwargs[0]["retry_backoff_seconds"],
            provider.settings.k8s_prometheus.retry_backoff_seconds,
        )
        self.assertEqual(
            FakePrometheusClient.init_kwargs[0]["range_query_chunk_hours"],
            provider.settings.k8s_prometheus.range_query_chunk_hours,
        )

    def test_fetch_target_can_use_incremental_history_hours(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=7,
            step_seconds=300,
            request_timeout_seconds=5,
            rate_window="5m",
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            provider._fetch_target(target, limit=0, history_hours=7)

        self.assertTrue(FakePrometheusClient.range_calls)
        windows = [call["end"] - call["start"] for call in FakePrometheusClient.range_calls]
        self.assertTrue(all(abs(window - 7 * 3600) < 5 for window in windows))

    def test_fetch_target_keeps_asymmetric_container_requests_and_limits_separate(self):
        target = PrometheusTarget(
            cluster="cluster-k8s-1",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", AsymmetricResourcePrometheusClient):
            items = provider._fetch_target(target, limit=0)

        self.assertEqual(len(items), 1)
        spec = items[0]["spec"]
        self.assertNotIn("cpu_request_cores", spec)
        self.assertNotIn("cpu_limit_cores", spec)
        self.assertNotIn("memory_request_gb", spec)
        self.assertNotIn("memory_limit_gb", spec)
        self.assertEqual(spec["cpu_limit_metric_mode"], "cpu_usage/cpu_limit")
        self.assertEqual(spec["cpu_request_metric_mode"], "cpu_usage_cores")
        self.assertEqual(spec["memory_limit_metric_mode"], "memory_working_set/memory_limit")
        self.assertEqual(spec["memory_request_metric_mode"], "memory_working_set/memory_request")
        self.assertAlmostEqual(spec["containers"]["alertmanager"]["memory_request_gb"], 200 / 1024)
        self.assertIsNone(spec["containers"]["alertmanager"]["cpu_limit_cores"])
        self.assertAlmostEqual(spec["containers"]["config-reloader"]["cpu_limit_cores"], 0.1)
        self.assertAlmostEqual(spec["containers"]["config-reloader"]["memory_limit_gb"], 25 / 1024)

        metrics = items[0]["metrics"]
        self.assertAlmostEqual(metrics["cpu_limit"]["values"][0], 0.1)
        self.assertAlmostEqual(metrics["cpu_limit"]["values"][1], 0.2)
        self.assertAlmostEqual(metrics["cpu_request"]["values"][0], 0.04)
        self.assertAlmostEqual(metrics["cpu_request"]["values"][1], 0.08)
        self.assertAlmostEqual(metrics["memory_limit"]["values"][0], 0.04)
        self.assertAlmostEqual(metrics["memory_limit"]["values"][1], 0.08)
        self.assertAlmostEqual(metrics["memory_request"]["values"][0], 0.05)
        self.assertAlmostEqual(metrics["memory_request"]["values"][1], 0.1)
        container_metrics = items[0]["container_metrics"]
        self.assertAlmostEqual(container_metrics["config-reloader"]["cpu_limit"]["values"][0], 0.1)
        self.assertAlmostEqual(container_metrics["config-reloader"]["cpu_limit"]["values"][1], 0.2)
        self.assertAlmostEqual(container_metrics["config-reloader"]["memory_limit"]["values"][0], 0.04)
        self.assertAlmostEqual(container_metrics["config-reloader"]["memory_limit"]["values"][1], 0.08)
        self.assertAlmostEqual(container_metrics["alertmanager"]["memory_request"]["values"][0], 0.05)
        self.assertAlmostEqual(container_metrics["alertmanager"]["memory_request"]["values"][1], 0.1)

    def test_data_quality_uses_seconds_for_gap_threshold(self):
        idx = pd.date_range("2026-01-01", periods=24, freq="300s")
        series = pd.Series([1.0] * len(idx), index=idx)

        quality = provider._data_quality(series, step_seconds=300)

        self.assertEqual(quality["level"], "good")
        self.assertEqual(quality["max_gap_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
