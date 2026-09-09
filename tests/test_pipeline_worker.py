from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.worker import iter_metric_inputs, worker
from resource_predict.resource_types import K8S_METRIC_NAMES


def _ctx() -> WorkerContext:
    return WorkerContext(
        test_size=1,
        future_steps=1,
        active_methods=["rolling_mean"],
        forecast_config={},
        metric_filter_by_id={},
        metric_partial_enabled=False,
        existing_partial_ids=set(),
        sample_interval_seconds=3600.0,
        max_interpolation_gap_steps=3,
    )


def test_worker_writes_observed_stats_for_full_history():
    index = pd.date_range("2026-01-01", periods=4, freq="h")
    source = {
        "resource_id": "vm-1",
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
        "cpu": pd.Series([1.0, 2.0, 3.0, 4.0], index=index),
        "memory": pd.Series([0.1, 0.2, 0.3, 0.4], index=index),
        "disk": pd.Series([0.5, 0.6, 0.7, 0.8], index=index),
    }

    def fake_fit_one_metric(_y_train, y_test, _y_full, *, ctx):
        future_index = pd.date_range(y_test.index[-1] + pd.Timedelta(hours=1), periods=ctx.future_steps, freq="h")
        pred = {"rolling_mean": pd.Series([float(y_test.iloc[-1])], index=y_test.index)}
        metrics = {"rolling_mean": {"rmse": 0.0, "selection_rmse": 0.0}}
        future = {"rolling_mean": pd.Series([float(y_test.iloc[-1])], index=future_index)}
        return pred, metrics, "rolling_mean", future, {"rolling_mean": 0.0}, {}

    with patch("resource_predict.pipeline.worker.fit_one_metric", side_effect=fake_fit_one_metric):
        item = worker(0, [source], ctx=_ctx(), parallel_metrics_enabled=False, inner_metric_workers=1)

    assert item["observed_stats"]["cpu"]["avg"] == 2.5
    assert item["observed_stats"]["cpu"]["peak"] == 4.0
    assert item["observed_stats"]["cpu"]["p95"] == 3.8499999999999996
    assert item["observed_stats"]["memory"]["p95"] == 0.385
    assert item["history_coverage"]["span_hours"] == 3.0
    assert item["history_coverage"]["span_days"] == 0.12
    assert item["history_coverage"]["is_short"] is True
    cpu_chart = item["charts_forecast"]["cpu"]
    assert cpu_chart["test_end_ms"] == int(index[-1].value // 1_000_000)
    assert cpu_chart["sample_interval_seconds"] == 3600.0
    assert cpu_chart["max_interpolation_gap_steps"] == 3


def _container_source():
    index = pd.date_range("2026-01-01", periods=4, freq="h")
    series = pd.Series([0.0, 1.0, 2.0, 0.0], index=index)
    return {
        "resource_id": "workload-1",
        "resource_type": "k8s_workload",
        "spec": {"containers": {"current": {
            "cpu_request_cores": 0.5, "cpu_limit_cores": 1.0,
            "memory_request_gb": 0.5, "memory_limit_gb": 1.0,
        }}},
        **{metric: series for metric in K8S_METRIC_NAMES},
        "container_metrics": {
            "current": {"cpu_limit": series * 0, "memory_request": series + 1},
            "historical": {"cpu_limit": series},
            "short": {"cpu_limit": series.iloc[:1]},
            "": {"cpu_limit": series},
            "invalid": None,
        },
        "observation_evidence": {"source": "fixture"},
        "container_metric_modes": {"current": {"cpu_limit": "explicit_zero"}},
        "container_data_quality": {"current": {"cpu_limit": {"missing": 0}}},
        "data_quality": {"missing": 0},
    }


def _fake_metric_fit(_train, test, series, *, ctx):
    future_index = pd.date_range(test.index[-1] + pd.Timedelta(hours=1),
                                 periods=ctx.future_steps, freq="h")
    value = float(series.iloc[-1])
    return (
        {"rolling_mean": test.copy()},
        {"rolling_mean": {"rmse": 0.0, "selection_rmse": 0.0}},
        "rolling_mean",
        {"rolling_mean": pd.Series(value, index=future_index)},
        {"rolling_mean": 0.25},
        {"evaluation": {"test_size": ctx.test_size}},
    )


@pytest.mark.parametrize("selection", [None, {"cpu_limit"}, set()])
def test_precomputed_worker_matches_serial_including_containers(selection):
    source, ctx = _container_source(), _ctx()
    if selection is not None:
        ctx.metric_partial_enabled = True
        ctx.existing_partial_ids = {source["resource_id"]}
        ctx.metric_filter_by_id = {source["resource_id"]: selection}
    inputs = list(iter_metric_inputs(source, ctx))
    aggregate = [("", "cpu_limit")] if selection else [("", metric) for metric in K8S_METRIC_NAMES]
    assert [(container, metric) for container, metric, _series in inputs] == aggregate + [
        ("current", "cpu_limit"), ("current", "memory_request"), ("historical", "cpu_limit")]
    assert all(isinstance(series, pd.Series) for _, _, series in inputs)
    precomputed = {(container, metric): _fake_metric_fit(
        series.iloc[:-ctx.test_size], series.iloc[-ctx.test_size:], series, ctx=ctx)
        for container, metric, series in inputs}
    with patch("resource_predict.pipeline.worker.fit_one_metric", side_effect=_fake_metric_fit) as fit:
        serial = worker(0, [source], ctx=ctx, parallel_metrics_enabled=False, inner_metric_workers=1)
        assert fit.call_count == len(inputs)
    with patch("resource_predict.pipeline.worker.fit_one_metric", side_effect=AssertionError("unexpected fit")), \
            patch("resource_predict.pipeline.worker.concurrent.futures.ThreadPoolExecutor",
                  side_effect=AssertionError("unexpected pool")):
        assembled = worker(0, [source], ctx=ctx, parallel_metrics_enabled=True,
                           inner_metric_workers=2, precomputed=precomputed)
    serial_timing = serial.pop("_timings")
    assembled_timing = assembled.pop("_timings")
    assert serial_timing["by_model"] == assembled_timing["by_model"]
    assert serial_timing["total"] == assembled_timing["total"]
    assert assembled == serial
    assert any(curve["container"] == "historical" for curve in assembled["_accuracy_holdout"])
    assert assembled["container_charts_forecast"]["current"]["cpu_limit"]["preds_future"]["rolling_mean"] == [0.0]


@pytest.mark.parametrize("missing", [("", "cpu_limit"), ("historical", "cpu_limit")])
def test_precomputed_worker_rejects_missing_result(missing):
    source, ctx = _container_source(), _ctx()
    precomputed = {(container, metric): _fake_metric_fit(
        series.iloc[:-ctx.test_size], series.iloc[-ctx.test_size:], series, ctx=ctx)
        for container, metric, series in iter_metric_inputs(source, ctx)}
    del precomputed[missing]
    with patch("resource_predict.pipeline.worker.fit_one_metric", side_effect=AssertionError("unexpected fit")):
        with pytest.raises(KeyError) as error:
            worker(0, [source], ctx=ctx, parallel_metrics_enabled=False,
                   inner_metric_workers=1, precomputed=precomputed)
    assert error.value.args == (missing,)
