import copy
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
import pandas as pd

from resource_predict.pipeline.calibration import calibrate_forecasts, refresh_calibration_advice
from resource_predict.pipeline.forecast_archive import archive_forecasts
from resource_predict.data.io import merge_charts_into_detail
from resource_predict.pipeline.run import generate_forecasts
from resource_predict.pipeline.partial import load_existing_forecast_items
from resource_predict.pipeline.realized_error import (
    DB_NAME, REPORT_NAME, _SCHEMA, _basis, _json, _unit, score_realized_forecasts,
)

T = 1_788_600_000_000


def item():
    return {"resource_id": "vm-a", "resource_type": "openstack_vm", "spec": {"cpu_cores": 2},
            "scaling_advice": {"action": "hold", "target_spec": {"cpu_cores": 2}},
            "forecast_diagnostics": {"cpu": {"provenance": {
                "data_end_ms": T, "generated_at_epoch_ms": T, "model_version": "v1", "config_hash": "cfg",
            }}},
            "charts_forecast": {"cpu": {"best_method": "rolling_mean", "x_pred_ms": [T + 1800000],
                                       "preds_future": {"rolling_mean": [0.2]}}}}


def seed(base, count=80, *, resource=None, residual=0.1, container="", repeat_targets=False):
    source = resource or item()
    with closing(sqlite3.connect(base / DB_NAME)) as db, db:
        db.executescript(_SCHEMA)
        for i in range(count):
            target = T - 1000 - (i if not repeat_targets else i % 10) * 600000
            batch = f"seed-{container}-{i}"
            db.execute("INSERT INTO batches VALUES (?,?)", (batch, target-900000))
            cursor = db.execute(
                "INSERT INTO curves(batch,resource_id,container,metric,model,unit,data_end_ms,issued_ms,"
                "basis,provenance,eligible) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (batch, source["resource_id"], container, "cpu", "rolling_mean", _unit(source, container, "cpu"),
                 target-1800000, target-900000, _basis(source, container, "cpu"),
                 _json({"model_version": "v1", "config_hash": "cfg"})),
            )
            db.execute("INSERT INTO points(curve_id,target_ms,predicted,actual,scored_at_ms) VALUES (?,?,?,?,?)",
                       (cursor.lastrowid, target, 0.2, 0.2+residual, target+1))


def bounds(source):
    return source["charts_forecast"]["cpu"]["calibration"]


def test_empirical_upper_annotation_preserves_action_and_predictions(tmp_path):
    seed(tmp_path)
    source = item()
    predictions = copy.deepcopy(source["charts_forecast"]["cpu"]["preds_future"])
    calibrate_forecasts(tmp_path, [source])
    assert bounds(source)["status"] == "calibrated"
    assert bounds(source)["upper"] == pytest.approx([0.3])
    assert bounds(source)["buckets"][0]["sample_count"] == 80
    assert source["charts_forecast"]["cpu"]["preds_future"] == predictions
    assert source["scaling_advice"]["target_spec"] == {"cpu_cores": 2}
    assert source["scaling_advice"]["action"] == "hold"
    assert source["scaling_advice"]["prediction_upper_bound"]["applied_to_targets"] is False


@pytest.mark.parametrize("change", ["resource", "spec", "model", "config", "version", "late", "future", "expired"])
def test_only_comparable_previously_known_errors_are_used(tmp_path, change):
    seed(tmp_path)
    source = item()
    if change == "resource":
        source["resource_id"] = "vm-b"
    elif change == "spec":
        source["spec"]["cpu_cores"] = 4
    elif change == "model":
        source["charts_forecast"]["cpu"].update(best_method="arima", preds_future={"arima": [0.2]})
    elif change in ("config", "version"):
        key = "config_hash" if change == "config" else "model_version"
        source["forecast_diagnostics"]["cpu"]["provenance"][key] = "new"
    else:
        with closing(sqlite3.connect(tmp_path / DB_NAME)) as db, db:
            if change == "late":
                db.execute("UPDATE points SET scored_at_ms=?", (T,))
            elif change == "future":
                source["forecast_diagnostics"]["cpu"]["provenance"]["data_end_ms"] = T - 86400000
            else:
                db.execute("UPDATE curves SET issued_ms=?", (T-8*86400000,))
    calibrate_forecasts(tmp_path, [source])
    assert bounds(source)["status"] == "insufficient_samples"
    assert bounds(source)["upper"] == [None]


def test_duplicate_targets_do_not_inflate_sample_count(tmp_path):
    seed(tmp_path, repeat_targets=True)
    source = item()
    calibrate_forecasts(tmp_path, [source])
    assert bounds(source)["buckets"][0]["sample_count"] == 10
    assert bounds(source)["upper"] == [None]


def test_partial_horizon_and_negative_residual_floor(tmp_path):
    seed(tmp_path, residual=-0.1)
    source = item()
    source["charts_forecast"]["cpu"]["x_pred_ms"].append(T+7200000)
    source["charts_forecast"]["cpu"]["preds_future"]["rolling_mean"].append(0.4)
    calibrate_forecasts(tmp_path, [source])
    assert bounds(source)["status"] == "partial"
    assert bounds(source)["upper"] == [0.2, None]
    assert source["scaling_advice"]["prediction_upper_bound"]["metrics"][0]["complete"] is False


def test_no_database_and_unreadable_database_fall_back(tmp_path):
    source = item()
    calibrate_forecasts(tmp_path, [source])
    assert bounds(source)["status"] == "insufficient_samples"
    assert not (tmp_path / DB_NAME).exists()
    (tmp_path / DB_NAME).write_bytes(b"invalid sqlite")
    calibrate_forecasts(tmp_path, [source])
    assert bounds(source)["status"] == "failed"
    assert source["scaling_advice"]["target_spec"] == {"cpu_cores": 2}


def test_calibrated_bound_is_archived_and_scored_without_recalibration(tmp_path):
    seed(tmp_path)
    source = item()
    calibrate_forecasts(tmp_path, [source])
    with patch("time.time", return_value=T/1000):
        archive_forecasts(tmp_path, [source])
    source["observation_evidence"] = {
        "schema_version": 1, "source": "test", "resource_type": "openstack_vm", "spec": source["spec"],
        "metrics": {"cpu": {"timestamps": [T+1800000], "values": [0.35]}},
    }
    with patch("time.time", return_value=(T+3600000)/1000):
        score_realized_forecasts(tmp_path, [source])
        score_realized_forecasts(tmp_path, [source])
    row, = json.loads((tmp_path / REPORT_NAME).read_text())["calibration_rows"]
    assert row["count"] == 1
    assert row["empirical_coverage"] == 0
    assert row["mean_exceedance"] == pytest.approx(0.05)
    assert row["mean_margin"] == pytest.approx(0.1)


def test_container_samples_are_isolated_and_advice_can_be_rebuilt(tmp_path):
    source = item()
    source["resource_type"] = "k8s_workload"
    source["spec"] = {"containers": {"app": {}, "sidecar": {}}}
    source["container_metric_modes"] = {name: {"cpu": "cpu_usage_cores"} for name in ("app", "sidecar")}
    chart = source.pop("charts_forecast")["cpu"]
    chart["forecast_diagnostics"] = source["forecast_diagnostics"]["cpu"]
    source["container_charts_forecast"] = {name: {"cpu": copy.deepcopy(chart)} for name in ("app", "sidecar")}
    seed(tmp_path, resource=source, container="app")
    calibrate_forecasts(tmp_path, [source])
    assert source["container_charts_forecast"]["app"]["cpu"]["calibration"]["status"] == "calibrated"
    assert source["container_charts_forecast"]["sidecar"]["cpu"]["calibration"]["status"] == "insufficient_samples"
    del source["scaling_advice"]["prediction_upper_bound"]
    refresh_calibration_advice([source])
    assert len(source["scaling_advice"]["prediction_upper_bound"]["metrics"]) == 2


def test_current_spec_change_does_not_relabel_old_bound(tmp_path):
    seed(tmp_path)
    source = item()
    calibrate_forecasts(tmp_path, [source])
    source["spec"]["cpu_cores"] = 8
    refresh_calibration_advice([source])
    summary = source["scaling_advice"]["prediction_upper_bound"]["metrics"][0]
    assert summary["status"] == "basis_changed"
    assert summary["upper_peak"] is None


def test_generation_partial_merge_and_detail_api_preserve_annotations(tmp_path):
    series = pd.Series([0.2]*48, index=pd.date_range(end=pd.Timestamp(T, unit="ms"), periods=48, freq="h"))
    provider_item = {"resource_id": "vm-a", "resource_type": "openstack_vm", "spec": {"cpu_cores": 2},
                     "metrics": {m: series for m in ("cpu", "memory", "disk")}}
    with patch("resource_predict.pipeline.run.read_forecast_config",
               return_value={"enabled_methods": ["rolling_mean"], "enable_ensemble": False}):
        generate_forecasts(out_dir=str(tmp_path), data_provider=lambda **kwargs: [provider_item],
                           test_size=4, future_steps=2, max_workers=1, save_raw=True)
        before, = load_existing_forecast_items(tmp_path)
        generate_forecasts(out_dir=str(tmp_path), predict_only=True, resource_ids=["vm-a"],
                           metric_names_by_resource={"vm-a": ["cpu"]}, test_size=4, future_steps=2, max_workers=1)
    after, = load_existing_forecast_items(tmp_path)
    assert after["charts_forecast"]["memory"]["calibration"] == before["charts_forecast"]["memory"]["calibration"]
    assert len(after["scaling_advice"]["prediction_upper_bound"]["metrics"]) == 3
    raw = {"resource_id": "vm-a", **{m: series for m in ("cpu", "memory", "disk")}}
    api = merge_charts_into_detail(after, {"vm-a": raw}, test_size=4)
    assert api["charts"]["cpu"]["calibration"] == after["charts_forecast"]["cpu"]["calibration"]
