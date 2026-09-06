import copy
import json

from resource_predict.pipeline.constants import GENERATION_STATS_FILENAME
from resource_predict.pipeline.write_outputs import _build_forecast_error_report, write_prediction_outputs


def _report(items):
    return _build_forecast_error_report(
        resources_items=items, active_methods=["arima", "rolling_mean"],
        test_size=12, future_steps=6, forecast_window={"sample_interval_seconds": 60},
    )


def _diagnostics(offset=0):
    return {
        "evaluation": {
            "role": "independent_test", "selection_status": "validated",
            "test_train_end_ms": offset + 100, "test_start_ms": offset + 101,
            "test_end_ms": offset + 112, "selected_method": "arima",
            "validation_windows": [
                {"train_end_ms": offset + 50, "validation_start_ms": offset + 51, "validation_end_ms": offset + 75},
                {"train_end_ms": offset + 75, "validation_start_ms": offset + 76, "validation_end_ms": offset + 100},
            ],
            "validation_metrics": {"arima": {"validation_rmse": 9, "validation_folds": 2}},
        },
        "provenance": {"data_end_ms": offset + 112, "config_hash": f"config-{offset}"},
    }


def test_distinct_resource_and_container_rows_with_actual_boundaries():
    item = {
        "resource_id": "workload-1", "resource_type": "k8s_workload",
        "metrics": {"cpu": {"arima": {"rmse": 1}}},
        "forecast_diagnostics": {"cpu": _diagnostics()},
        "container_charts_forecast": {
            "app": {"cpu": {"metrics": {"arima": {"rmse": 2}}, "forecast_diagnostics": _diagnostics(1000)}},
            "sidecar": {"cpu": {"metrics": {"arima": {"rmse": 3}}, "forecast_diagnostics": _diagnostics(2000)}},
        },
    }
    before = copy.deepcopy(item)
    report = _report([item])
    assert report["meta"]["resources"] == 1
    assert report["meta"]["rows"] == 3
    rows = {row["container"]: row for row in report["rows"]}
    assert rows[None]["rmse"] == 1
    assert rows["app"]["rmse"] == 2
    assert rows["sidecar"]["rmse"] == 3
    assert rows["app"]["window"]["test_start_ms"] == 1101
    assert rows["app"]["window"]["test_train_end_ms"] == 1100
    assert rows["app"]["window"]["validation_start_ms"] == 1051
    assert rows["app"]["window"]["validation_end_ms"] == 1100
    assert rows["app"]["provenance"]["config_hash"] == "config-1000"
    assert rows["app"]["evaluation_role"] == "independent_test"
    resource = report["resources"][0]
    assert resource["metrics"]["cpu"]["arima"]["rmse"] == 1
    assert resource["container_metrics"]["app"]["cpu"]["arima"]["rmse"] == 2
    assert len(resource["container_evaluation"]["app"]["cpu"]["validation_windows"]) == 2
    assert "validation_metrics" not in resource["evaluation"]["cpu"]
    assert "validation_windows" not in rows["app"]["window"]
    assert item == before


def test_legacy_metrics_preserved_and_not_mislabeled_independent():
    values = {"rmse": 1, "mae": 2, "mape": 3, "p95_error": 4,
              "selection_rmse": 5, "rolling_rmse": 6, "rolling_mae": 7, "rolling_folds": 8}
    row, = _report([{"resource_id": "vm-1", "metrics": {"cpu": {"arima": values}}}])["rows"]
    assert all(row[key] == value for key, value in values.items())
    assert row["evaluation_role"] == "legacy_holdout"
    assert row["selection_status"] == "legacy_unknown"
    assert row["container"] is None
    assert row["validation_rmse"] is None


def test_failed_models_keep_null_test_errors_and_validation_evidence():
    diagnostics = _diagnostics()
    diagnostics["phase_failures"] = {
        "test": {"arima": "test failure"},
        "validation": {"prophet": "not fitted"},
        "future": {"rolling_mean": "future failure"},
    }
    metrics = {"rolling_mean": {"rmse": 2, "validation_mae": 3, "validation_mape": 4,
                                "validation_p95_error": 5, "validation_folds": 2}}
    report = _report([{"resource_id": "vm-1", "metrics": {"cpu": metrics},
                       "forecast_diagnostics": {"cpu": diagnostics}}])
    rows = {row["model"]: row for row in report["rows"]}
    assert set(rows) == {"arima", "prophet", "rolling_mean"}
    assert rows["arima"]["rmse"] is None
    assert rows["arima"]["validation_rmse"] == 9
    assert rows["arima"]["phase_failures"] == {"test": "test failure"}
    assert rows["prophet"]["rmse"] is None
    assert rows["prophet"]["phase_failures"] == {"validation": "not fitted"}
    assert rows["rolling_mean"]["rmse"] == 2
    assert rows["rolling_mean"]["validation_p95_error"] == 5
    assert rows["rolling_mean"]["phase_failures"] == {"future": "future failure"}


def test_container_only_and_duplicate_resource_ids_count_uniquely():
    item = {"resource_id": "workload-1", "container_charts_forecast": {
        "app": {"cpu": {"metrics": {"arima": {"rmse": float("nan")}}}},
    }}
    report = _report([item, item])
    assert report["meta"]["resources"] == 1
    assert report["meta"]["rows"] == 2
    assert report["rows"][0]["rmse"] is None


def test_archive_failure_metadata_is_visible_in_generation_stats(tmp_path):
    archive = {"status": "failed", "path": None, "count": 0, "error": "disk full"}
    write_prediction_outputs(
        out_base=tmp_path, resources_items=[], active_methods=["arima"],
        test_size=12, future_steps=6, forecast_window={}, detail_chunk_size=10,
        predicted_count=0, partial_resource_ids=set(), metric_filter_by_id={},
        metric_partial_enabled=False, total_elapsed=0, raw_stats={},
        forecast_archive=archive,
    )
    stats = json.loads((tmp_path / GENERATION_STATS_FILENAME).read_text(encoding="utf-8"))
    assert stats["forecast_archive"] == archive
