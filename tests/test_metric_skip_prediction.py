import copy
import json

import pandas as pd
import pytest

from resource_predict.data.io import merge_charts_into_detail
from resource_predict.data.raw_store import RawResourceStore
from resource_predict.pipeline.run import generate_forecasts, generate_predictions_only
from resource_predict.pipeline.partial import load_existing_forecast_items
from resource_predict.resource_types import K8S_METRIC_NAMES
from resource_predict.settings import settings

pytestmark = pytest.mark.usefixtures("pipeline")

@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr("resource_predict.pipeline.run.read_forecast_config",
                        lambda: {"enabled_methods": ["rolling_mean"], "enable_ensemble": False})


def source(start="2026-09-01", points=20):
    values = pd.Series(.3, index=pd.date_range(start, periods=points, freq=f"{settings.k8s_prometheus.step_seconds}s"))
    return {"resource_id": "k8s:a:ns:deployment:api", "resource_type": "k8s_workload",
            "spec": {"containers": {"app": {}}},
            "metrics": {metric: values.copy() for metric in K8S_METRIC_NAMES},
            "container_metrics": {"app": {metric: values.copy() for metric in K8S_METRIC_NAMES}}}


def generate(path, item, backend="serial"):
    return generate_forecasts(out_dir=str(path), data_provider=lambda **_: [item],
                              test_size=6, future_steps=3, parallel_backend=backend, max_workers=2, save_raw=True)[0]


@pytest.mark.parametrize("with_old", [False, True])
def test_only_short_metrics_are_skipped_and_old_evidence_is_preserved(tmp_path, with_old):
    old = generate(tmp_path, source()) if with_old else None
    item = source("2026-09-02")
    item["metrics"]["memory_request"] = item["metrics"]["memory_request"].iloc[-6:]
    item["container_metrics"]["app"]["cpu_limit"] = item["container_metrics"]["app"]["cpu_limit"].iloc[-6:]
    new = generate(tmp_path, item, backend="process" if with_old else "serial")
    assert new["prediction_status"] == "partial_success"
    assert new["charts_forecast"]["cpu_limit"]["preds_future"]
    assert new["container_charts_forecast"]["app"]["memory_request"]["preds_future"]
    assert new["scaling_advice"]["action_gate"]["state"] == "blocked"
    if with_old:
        assert new["charts_forecast"]["memory_request"] == old["charts_forecast"]["memory_request"]
        assert new["forecast_diagnostics"]["memory_request"] == old["forecast_diagnostics"]["memory_request"]
        assert new["container_charts_forecast"]["app"]["cpu_limit"] == old["container_charts_forecast"]["app"]["cpu_limit"]
    else:
        assert new["charts_forecast"]["memory_request"] == {}
    raw = RawResourceStore(tmp_path).get(item["resource_id"])
    detail = merge_charts_into_detail(new, {item["resource_id"]: raw}, test_size=6)
    assert detail["charts"]["memory_request"]["prediction_skipped"]
    assert detail["charts"]["memory_request"]["forecast_status"] == ("retained" if with_old else "unavailable")
    assert not detail["charts"]["cpu_limit"]["prediction_skipped"]
    assert detail["container_charts"]["app"]["cpu_limit"]["prediction_skipped"]
    stats = json.loads((tmp_path / "generation_stats.json").read_text(encoding="utf-8"))
    assert stats["predicted_resources"] == 1
    assert stats["execution"]["completed"] == 6
    report = json.loads((tmp_path / "forecast_error_report.json").read_text(encoding="utf-8"))
    assert any(row["metric"] == "cpu_limit" and row["forecast_status"] == "updated" for row in report["rows"])
    if with_old:
        assert any(row["metric"] == "memory_request" and row["container"] is None
                   and row["forecast_status"] == "retained" for row in report["rows"])


def test_short_aggregates_do_not_block_healthy_container(tmp_path):
    item = source()
    item["metrics"] = {m: s.iloc[-6:] for m, s in item["metrics"].items()}
    new = generate(tmp_path, item)
    assert new["prediction_status"] == "partial_success"
    assert all(chart["preds_future"] for chart in new["container_charts_forecast"]["app"].values())
    assert all(chart == {} for chart in new["charts_forecast"].values())


def test_all_short_without_previous_forecast_still_publishes_history(tmp_path):
    item = source(points=6)
    new = generate(tmp_path, item)
    assert new["prediction_status"] == "skipped"
    raw = RawResourceStore(tmp_path).get(item["resource_id"])
    detail = merge_charts_into_detail(new, {item["resource_id"]: raw}, test_size=6)
    assert len(detail["charts"]["cpu_limit"]["y_train"]) == 6
    assert not detail["charts"]["cpu_limit"]["preds_future"]
    assert detail["charts"]["cpu_limit"]["forecast_status"] == "unavailable"


def test_local_partial_merge_retains_only_short_metric_and_recovers(tmp_path):
    old = generate(tmp_path, source())
    item = source("2026-09-02")
    item["metrics"]["memory_request"] = item["metrics"]["memory_request"].iloc[-6:]
    generate(tmp_path, item)
    result = generate_predictions_only(out_dir=str(tmp_path), resource_ids=[item["resource_id"]],
        metric_names_by_resource={item["resource_id"]: ["cpu_limit", "memory_request"]},
        test_size=6, future_steps=3, parallel_backend="serial", local_update=True)[0]
    assert result["charts_forecast"]["memory_request"] == old["charts_forecast"]["memory_request"]
    assert result["prediction_status"] == "partial_success"
    assert load_existing_forecast_items(tmp_path)[0]["data_quality"]["memory_request"]["prediction_skipped"]
    healthy = copy.deepcopy(item)
    healthy["metrics"]["memory_request"] = healthy["metrics"]["cpu_limit"].copy()
    recovered = generate(tmp_path, healthy)
    assert recovered["prediction_status"] == "success"
    assert not recovered["data_quality"]["memory_request"]["prediction_skipped"]
    assert recovered["charts_forecast"]["memory_request"]["x_test_ms"] != old["charts_forecast"]["memory_request"]["x_test_ms"]
