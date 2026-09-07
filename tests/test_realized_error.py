import copy
import json
import sqlite3
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import pandas as pd
import pytest

from resource_predict.data.io import prepared_dict_to_raw_record, raw_record_to_prepared
from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from resource_predict.data.updater import _build_new_resource_from_upsert, run_upsert_with_data
from resource_predict.pipeline.forecast_archive import archive_forecasts
from resource_predict.pipeline.realized_error import DB_NAME, REPORT_NAME, score_realized_forecasts

T = 1_788_600_000_000
HOUR = 3_600_000


def resource():
    provenance = {"data_end_ms": T, "generated_at_epoch_ms": T, "config_hash": "original"}
    chart = {"best_method": "rolling_mean", "x_pred_ms": [T + HOUR, T + 2 * HOUR],
             "preds_future": {"rolling_mean": [0.2, 0.8]},
             "forecast_diagnostics": {"provenance": provenance}}
    return {"resource_id": "vm-a", "resource_type": "openstack_vm", "spec": {"cpu_cores": 2},
            "charts_forecast": {"cpu": chart}, "forecast_diagnostics": {"cpu": {"provenance": provenance}}}


def evidence(item, targets=(T + HOUR,), values=(0.4,)):
    result = copy.deepcopy(item)
    result["observation_evidence"] = {
        "schema_version": 1, "source": "test_unfilled", "resource_type": item["resource_type"],
        "spec": copy.deepcopy(item["spec"]),
        "container_metric_modes": item.get("container_metric_modes", {}),
        "metrics": {"cpu": {"timestamps": list(targets), "values": list(values)}},
    }
    return result


def archive(base, item):
    with patch("time.time", return_value=T / 1000):
        return archive_forecasts(base, [item])


def score(base, items=(), now=T + 2 * HOUR):
    with patch("time.time", return_value=now / 1000):
        return score_realized_forecasts(base, items)


def query(base, sql):
    with closing(sqlite3.connect(base / DB_NAME)) as db:
        return db.execute(sql).fetchall()


def test_idempotent_late_backfill_and_original_prediction(tmp_path):
    item = resource()
    metadata = archive(tmp_path, item)
    original = Path(metadata["path"]).read_bytes()
    assert score(tmp_path, [evidence(item)])["newly_scored"] == 1
    assert score(tmp_path, [evidence(item, values=(0.9,))])["newly_scored"] == 0
    result = score(tmp_path, [evidence(item, (T + 2 * HOUR,), (0.6,))])
    assert result["newly_scored"] == 1
    assert result["coverage"] == {"scored": 2}
    assert query(tmp_path, "SELECT predicted,actual FROM points ORDER BY target_ms") == [(0.2, 0.4), (0.8, 0.6)]
    assert Path(metadata["path"]).read_bytes() == original
    report = json.loads((tmp_path / REPORT_NAME).read_text())
    assert [row["horizon"] for row in report["rows"]] == ["0-1h", "1-6h"]
    assert report["rows"][0]["rmse"] == pytest.approx(0.2)
    assert report["rows"][0]["underestimate_rate"] == 1
    assert report["rows"][1]["mean_underestimate"] == 0


def test_missing_unfilled_future_and_nonfinite_observations(tmp_path):
    item = resource()
    archive(tmp_path, item)
    assert score(tmp_path, [item])["resources_without_evidence"] == 1
    # A near timestamp must not be rounded onto the forecast target.
    assert score(tmp_path, [evidence(item, (T + HOUR + 1,), (0.4,))])["newly_scored"] == 0
    assert score(tmp_path, [evidence(item)], now=T + HOUR - 1)["newly_scored"] == 0
    result = score(tmp_path, [evidence(item, values=(float("nan"),))])
    assert result["coverage"]["nonfinite_observation"] == 1
    assert score(tmp_path, [evidence(item)])["newly_scored"] == 1


def test_basis_change_is_not_scored_but_can_be_retried(tmp_path):
    item = resource()
    archive(tmp_path, item)
    incoming = evidence(item)
    incoming["observation_evidence"]["spec"]["cpu_cores"] = 4
    assert score(tmp_path, [incoming])["coverage"]["basis_mismatch"] == 1
    assert score(tmp_path, [evidence(item)])["newly_scored"] == 1


def test_container_and_scope_isolation(tmp_path):
    item = resource()
    item.update(resource_type="k8s_workload", spec={"containers": {"app": {"cpu_limit_cores": 1},
                                                                  "sidecar": {"cpu_limit_cores": 1}}})
    item["container_metric_modes"] = {name: {"cpu": "cpu_usage/cpu_limit"} for name in ("app", "sidecar")}
    chart = item.pop("charts_forecast")["cpu"]
    item["container_charts_forecast"] = {name: {"cpu": chart} for name in ("app", "sidecar")}
    archive(tmp_path / "k8s", item)
    incoming = evidence(item)
    block = incoming["observation_evidence"].pop("metrics")
    incoming["observation_evidence"]["container_metrics"] = {"app": block}
    assert score(tmp_path / "k8s", [incoming])["newly_scored"] == 1
    assert query(tmp_path / "k8s", "SELECT c.container FROM curves c JOIN points p ON c.id=p.curve_id WHERE actual IS NOT NULL") == [("app",)]
    assert score(tmp_path / "vm", [incoming])["status"] == "no_archives"


def test_historical_recalculation_is_excluded(tmp_path):
    item = resource()
    item["forecast_diagnostics"]["cpu"]["provenance"]["generated_at_epoch_ms"] = T + HOUR
    archive(tmp_path, item)
    assert score(tmp_path, [evidence(item)])["coverage"]["not_future_at_publication"] == 1


def test_retention_and_archive_import_rollback(tmp_path):
    item = resource()
    metadata = archive(tmp_path, item)
    path = Path(metadata["path"])
    original = path.read_bytes()
    path.write_bytes(original[:20])
    with pytest.raises((EOFError, OSError)):
        score(tmp_path)
    assert query(tmp_path, "SELECT COUNT(*) FROM batches") == [(0,)]
    path.write_bytes(original)
    assert score(tmp_path, [evidence(item)])["newly_scored"] == 1
    score(tmp_path, now=T + 8 * 24 * HOUR)
    assert query(tmp_path, "SELECT COUNT(*) FROM points") == [(0,)]


def test_raw_round_trip_and_automatic_scoring(tmp_path):
    item = resource()
    archive(tmp_path, item)
    prepared = evidence(item)
    for metric in ("cpu", "memory", "disk"):
        prepared[metric] = pd.Series([0.4], index=pd.to_datetime([T + HOUR], unit="ms"))
    roundtrip = raw_record_to_prepared(prepared_dict_to_raw_record(prepared))
    assert roundtrip["observation_evidence"] == prepared["observation_evidence"]
    with patch("time.time", return_value=(T + 2 * HOUR) / 1000):
        write_raw_resource_dataset(tmp_path, [prepared], freq="h")
    assert query(tmp_path, "SELECT COUNT(*) FROM points WHERE actual IS NOT NULL") == [(1,)]
    assert RawResourceStore(tmp_path).get("vm-a")["observation_evidence"] == prepared["observation_evidence"]


def test_upsert_preserves_evidence_and_backfills(tmp_path):
    item = resource()
    archive(tmp_path, item)
    incoming = evidence(item)
    incoming["metrics"] = {m: {"timestamps": [T + HOUR], "values": [0.4]} for m in ("cpu", "memory", "disk")}
    assert _build_new_resource_from_upsert(incoming)["observation_evidence"] == incoming["observation_evidence"]
    with patch("time.time", return_value=(T + 2 * HOUR) / 1000), patch(
        "resource_predict.pipeline.generate_predictions_only", return_value=[]
    ):
        assert run_upsert_with_data([incoming], out_dir=tmp_path)["success"]
        later = evidence(item, (T + 2 * HOUR,), (0.6,))
        later["metrics"] = {m: {"timestamps": [T + 2 * HOUR], "values": [0.6]} for m in ("cpu", "memory", "disk")}
        assert run_upsert_with_data([later], out_dir=tmp_path)["success"]
    assert query(tmp_path, "SELECT COUNT(*) FROM points WHERE actual IS NOT NULL") == [(2,)]


def test_report_failure_keeps_committed_scores_and_retries(tmp_path):
    item = resource()
    archive(tmp_path, item)
    with patch("resource_predict.pipeline.realized_error.atomic_write_json", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            score(tmp_path, [evidence(item)])
    assert query(tmp_path, "SELECT actual FROM points WHERE actual IS NOT NULL") == [(0.4,)]
    assert score(tmp_path, [evidence(item)])["newly_scored"] == 0
    assert json.loads((tmp_path / REPORT_NAME).read_text())["coverage"]["scored"] == 1


def test_scoring_failure_does_not_undo_raw_commit(tmp_path):
    prepared = evidence(resource())
    for metric in ("cpu", "memory", "disk"):
        prepared[metric] = pd.Series([0.4], index=pd.to_datetime([T + HOUR], unit="ms"))
    with patch("resource_predict.pipeline.realized_error.score_realized_forecasts", side_effect=OSError("locked")):
        write_raw_resource_dataset(tmp_path, [prepared], freq="h")
    assert RawResourceStore(tmp_path).get("vm-a")["cpu"].iloc[0] == 0.4


def test_changed_container_members_and_metric_mode_are_excluded(tmp_path):
    item = resource()
    item.update(resource_type="k8s_workload", spec={"containers": {"app": {}}, "pods_observed": ["a"]})
    item["container_metric_modes"] = {"app": {"cpu": "cpu_usage_cores"}}
    chart = item.pop("charts_forecast")["cpu"]
    item["container_charts_forecast"] = {"app": {"cpu": chart}}
    archive(tmp_path, item)
    incoming = evidence(item)
    proof = incoming["observation_evidence"]
    proof["container_metrics"] = {"app": proof.pop("metrics")}
    proof["spec"]["pods_observed"] = ["b"]
    assert score(tmp_path, [incoming])["coverage"]["basis_mismatch"] == 1
    proof["spec"]["pods_observed"] = ["a"]
    proof["container_metric_modes"] = {"app": {"cpu": "cpu_usage/cpu_limit"}}
    assert score(tmp_path, [incoming])["coverage"]["basis_mismatch"] == 1
