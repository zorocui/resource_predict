from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.worker import worker


def _ctx() -> WorkerContext:
    return WorkerContext(
        test_size=1,
        future_steps=1,
        active_methods=["rolling_mean"],
        forecast_config={},
        metric_filter_by_id={},
        metric_partial_enabled=False,
        existing_partial_ids=set(),
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
