from copy import deepcopy
from unittest.mock import patch

from flask import Flask

from resource_predict.services.accuracy_summary import write_accuracy_summary, read_accuracy_summary
from resource_predict.api.forecast_accuracy import register_forecast_accuracy_routes


def item():
    return dict(resource_id="vm-a", resource_type="openstack_vm",
                charts_forecast={"cpu": {"best_method": "baseline"}},
                _accuracy_holdout=[dict(container="", metric="cpu", model="baseline",
                    x_test_ms=[2, 3, 4], actual=[0.5, 0.5, None], yhat=[0.55, 0.7, 0.4],
                    evaluation={"role": "independent_test", "test_train_end_ms": 1})])


def test_selected_model_threshold_invalid_and_no_sqlite(tmp_path):
    resource = item()
    other = deepcopy(resource["_accuracy_holdout"][0])
    other.update(model="unselected", yhat=[0.5, 0.5, 0.5])
    resource["_accuracy_holdout"].append(other)
    write_accuracy_summary(tmp_path, [resource])
    report = read_accuracy_summary([tmp_path])
    assert report["accuracy"] == 0.5
    assert report["valid_points"] == 2
    assert report["invalid_points"] == 1
    assert len(report["rows"]) == 1
    assert not list(tmp_path.glob("*.sqlite3"))
    # Latest run replaces prior results, rather than accumulating duplicate points.
    write_accuracy_summary(tmp_path, [])
    assert read_accuracy_summary([tmp_path])["accuracy"] is None


def test_container_preference_and_absolute_units(tmp_path):
    resource = item()
    resource.update(resource_id="k8s:a", resource_type="k8s_workload",
                    container_charts_forecast={"app": resource["charts_forecast"]},
                    container_metric_modes={"app": {"cpu": "cpu_usage_cores"}})
    curve = deepcopy(resource["_accuracy_holdout"][0])
    curve["container"] = "app"
    resource["_accuracy_holdout"].append(curve)
    write_accuracy_summary(tmp_path, [resource])
    report = read_accuracy_summary([tmp_path])
    assert len(report["rows"]) == 1
    assert report["accuracy"] is None
    assert report["absolute_unit_points"] == 2


def test_non_independent_and_corrupt_file(tmp_path):
    resource = item()
    resource["_accuracy_holdout"][0]["evaluation"]["test_train_end_ms"] = 2
    write_accuracy_summary(tmp_path, [resource])
    assert read_accuracy_summary([tmp_path])["valid_points"] == 0
    app = Flask(__name__)
    register_forecast_accuracy_routes(app, lambda: [tmp_path])
    assert app.test_client().get("/api/forecast-accuracy/summary").status_code == 200
    (tmp_path / "forecast_accuracy_summary.json").write_text("broken", encoding="utf-8")
    assert app.test_client().get("/api/forecast-accuracy/summary").status_code == 503




def test_prediction_automatically_publishes_summary_without_ledger(tmp_path):
    import pandas as pd
    from resource_predict.pipeline import generate_forecasts
    values = pd.Series([0.2] * 48, index=pd.date_range("2026-09-01", periods=48, freq="h"))
    resource = dict(resource_id="vm-a", resource_type="vm", spec={"cpu_cores": 2, "memory_gb": 4, "disk_gb": 50},
                    metrics={metric: values for metric in ("cpu", "memory", "disk")})
    with patch("resource_predict.pipeline.run.read_forecast_config",
               return_value={"enabled_methods": ["rolling_mean"], "enable_ensemble": False}):
        generate_forecasts(out_dir=str(tmp_path), test_size=4, future_steps=2, max_workers=1,
                           data_provider=lambda **kwargs: [resource], save_raw=True)
    report = read_accuracy_summary([tmp_path])
    assert report["accuracy"] == 1
    assert report["valid_points"] == 12
    assert report["resource_count"] == 1
    assert not (tmp_path / "forecast_realized.sqlite3").exists()
    assert not (tmp_path / "forecast_history").exists()
