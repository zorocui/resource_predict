import copy
import gzip
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from resource_predict.pipeline.forecast_archive import archive_forecasts
from resource_predict.pipeline.realized_error import DB_NAME, score_realized_forecasts
from resource_predict.pipeline.worker import _holdout_curves


T = 1_788_600_000_000


def item():
    return {"resource_id": "workload-a", "resource_type": "k8s_workload",
            "spec": {"cpu_metric_mode": "absolute"},
            "container_metric_modes": {"app": {"cpu": "absolute"}},
            "_accuracy_holdout": [{
                "container": "app", "metric": "cpu", "model": "rolling_mean",
                "x_test_ms": [T + 1000, T + 2000], "yhat": [0.2, None], "actual": [0.0, 0.8],
                "provenance": {"actual_source": "prepared_history", "generated_at_epoch_ms": T + 2000},
                "evaluation": {"role": "independent_test", "test_train_end_ms": T,
                               "test_start_ms": T + 1000, "test_end_ms": T + 2000,
                               "selection_status": "validated", "selected_method": "rolling_mean",
                               "validation_metrics": {"rolling_mean": {"selection_rmse": 0.1}},
                               "routing_train_end_ms": T - 3000,
                               "validation_windows": [{"train_end_ms": T - 2000,
                                                       "validation_start_ms": T - 1000,
                                                       "validation_end_ms": T}]}}]}


def archive_score(base, resource):
    with patch("time.time", return_value=(T + 3000) / 1000):
        metadata = archive_forecasts(base, [resource])
        score_realized_forecasts(base, publish_report=False)
    return metadata


def query(base, sql):
    with closing(sqlite3.connect(base / DB_NAME)) as db:
        return db.execute(sql).fetchall()


def test_frozen_all_models_containers_and_retention(tmp_path):
    resource = item()
    second = copy.deepcopy(resource["_accuracy_holdout"][0])
    second.update(container="", model="seasonal_naive", yhat=[0.3, 0.6])
    resource["_accuracy_holdout"].append(second)
    metadata = archive_score(tmp_path, resource)
    with gzip.open(metadata["path"], "rt", encoding="utf-8") as stream:
        assert json.load(stream)["holdout_forecasts"] == resource["_accuracy_holdout"]
    original = query(tmp_path, "SELECT target_ms,predicted,actual,skip_reason FROM holdout_points ORDER BY curve_id,target_ms")
    assert original[:2] == [(T + 1000, 0.2, 0.0, None), (T + 2000, None, 0.8, "invalid_prediction")]
    assert query(tmp_path, "SELECT container,model,data_end_ms,eligible FROM holdout_curves ORDER BY id") == [
        ("app", "rolling_mean", T, 1), ("", "seasonal_naive", T, 1)]
    resource["_accuracy_holdout"][0]["actual"] = [900, 900]
    with patch("time.time", return_value=(T + 4000) / 1000):
        score_realized_forecasts(tmp_path, [resource], publish_report=False)
    assert query(tmp_path, "SELECT target_ms,predicted,actual,skip_reason FROM holdout_points ORDER BY curve_id,target_ms") == original
    with patch("time.time", return_value=T / 1000 + 8 * 86400):
        score_realized_forecasts(tmp_path, publish_report=False)
    assert query(tmp_path, "SELECT COUNT(*) FROM holdout_curves") == [(0,)]
    assert query(tmp_path, "SELECT COUNT(*) FROM holdout_points") == [(0,)]


@pytest.mark.parametrize("field,value,reason", [
    ("role", "validation", "not_independent_test"),
    ("test_train_end_ms", T + 1000, "invalid_test_boundary"),
    ("test_end_ms", T + 4000, "invalid_test_boundary"),
    ("selection_status", "validation_failed", "unvalidated_selection"),
    ("routing_train_end_ms", T + 1000, "invalid_selection_boundary"),
])
def test_bad_independence_is_not_scored(tmp_path, field, value, reason):
    resource = item()
    resource["_accuracy_holdout"][0]["evaluation"][field] = value
    archive_score(tmp_path, resource)
    assert query(tmp_path, "SELECT eligible FROM holdout_curves") == [(0,)]
    assert query(tmp_path, "SELECT DISTINCT skip_reason FROM holdout_points") == [(reason,)]


def test_timestamp_alignment_and_missing_predictions():
    truth = pd.Series([0.0, 0.7], index=pd.to_datetime([T + 1000, T + 2000], unit="ms"))
    prediction = pd.Series([0.8, 0.1], index=truth.index[::-1])
    diagnostics = {"evaluation": {"role": "independent_test"},
                   "phase_failures": {"test": {"failed_model": "failure"}}}
    rows = _holdout_curves({}, "app", "cpu", truth, {"ok": prediction}, diagnostics)
    assert rows[0]["yhat"] == [0.1, 0.8]
    assert rows[0]["actual"] == [0.0, 0.7]
    assert rows[1]["model"] == "failed_model" and rows[1]["yhat"] == [None, None]
    truth.iloc[0] = np.nan
    assert rows[0]["actual"] == [0.0, 0.7]


def test_legacy_has_no_synthetic_holdout(tmp_path):
    resource = item()
    resource.pop("_accuracy_holdout")
    resource["charts_forecast"] = {"cpu": {"best_method": "rolling_mean", "x_pred_ms": [T + 5000],
                                           "preds_future": {"rolling_mean": [0.2]}}}
    archive_score(tmp_path, resource)
    assert query(tmp_path, "SELECT COUNT(*) FROM holdout_points") == [(0,)]


def test_missing_provenance_is_retained_unscored(tmp_path):
    resource = item()
    resource["_accuracy_holdout"][0]["provenance"] = None
    archive_score(tmp_path, resource)
    assert query(tmp_path, "SELECT DISTINCT skip_reason FROM holdout_points") == [("missing_provenance",)]


def test_holdout_batch_import_is_atomic(tmp_path):
    resource = item()
    broken = copy.deepcopy(resource["_accuracy_holdout"][0])
    broken.update(model="broken", yhat=[1])
    resource["_accuracy_holdout"].append(broken)
    with pytest.raises(ValueError, match="unaligned holdout"):
        archive_score(tmp_path, resource)
    assert query(tmp_path, "SELECT COUNT(*) FROM batches") == [(0,)]
    assert query(tmp_path, "SELECT COUNT(*) FROM holdout_points") == [(0,)]
