import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resource_predict.data.io import prepared_dict_to_raw_record
from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from resource_predict.pipeline.run import generate_forecasts, generate_predictions_only
from resource_predict.services.store.forecast_store import _SingleForecastStore
from resource_predict.settings import AppConfig, GenerationConfig


def _resource(resource_id, base):
    index = pd.date_range("2026-01-01", periods=8, freq="h")
    values = pd.Series([base + i / 100 for i in range(8)], index=index)
    return {
        "resource_id": resource_id,
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
        "cpu": values,
        "memory": values,
        "disk": values,
    }


def _forecast_item(resource_id):
    block = {
        "preds": {"rolling_mean": [0.2, 0.2]},
        "x_pred_ms": [1, 2],
        "preds_future": {"rolling_mean": [0.2]},
        "metrics": {"rolling_mean": {"rmse": 0.1}},
        "best_method": "rolling_mean",
    }
    return {
        "resource_id": resource_id,
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
        "scaling_advice": {"action": "hold"},
        "charts_forecast": {"cpu": block, "memory": block, "disk": block},
    }


class ForecastStoreTest(unittest.TestCase):
    def _write_artifacts(self, base):
        write_raw_resource_dataset(base, [_resource("vm-1", 0.1), _resource("vm-2", 0.2)], freq="h")
        details = {"resources": [_forecast_item("vm-1"), _forecast_item("vm-2")]}
        (base / "details").mkdir(parents=True)
        (base / "details" / "part-00000.json").write_text(json.dumps(details), encoding="utf-8")
        summary = {
            "meta": {"test_size": 2},
            "resources": [
                {"resource_id": "vm-1", "spec": {}, "detail_ref": {"file": "part-00000.json", "offset": 0}},
                {"resource_id": "vm-2", "spec": {}, "detail_ref": {"file": "part-00000.json", "offset": 1}},
            ],
        }
        (base / "summary_index.json").write_text(json.dumps(summary), encoding="utf-8")

    def test_metadata_detail_does_not_read_raw_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_artifacts(base)
            ref = RawResourceStore(base).raw_ref("vm-1")
            (base / Path(*ref["file"].split("/"))).write_text("{broken", encoding="utf-8")
            store = _SingleForecastStore(AppConfig(out_dir=str(base)), GenerationConfig())

            detail = store.get_resource_detail("vm-1", include_charts=False)

            self.assertEqual(detail["resource_id"], "vm-1")
            self.assertNotIn("charts_forecast", detail)

    def test_chart_detail_reads_only_target_and_limits_training_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_artifacts(base)
            other_ref = RawResourceStore(base).raw_ref("vm-2")
            (base / Path(*other_ref["file"].split("/"))).write_text("{broken", encoding="utf-8")
            store = _SingleForecastStore(AppConfig(out_dir=str(base)), GenerationConfig())

            detail = store.get_resource_detail("vm-1", history_points=3, metric="cpu")

            self.assertEqual(len(detail["charts"]["cpu"]["y_train"]), 3)
            self.assertEqual(len(detail["charts"]["cpu"]["y_test"]), 2)
            self.assertNotIn("memory", detail["charts"])

    def test_chart_detail_filters_training_history_by_time_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_artifacts(base)
            store = _SingleForecastStore(AppConfig(out_dir=str(base)), GenerationConfig())
            start_ms = int(pd.Timestamp("2026-01-01 03:00:00").value // 1_000_000)
            end_ms = int(pd.Timestamp("2026-01-01 04:00:00").value // 1_000_000)

            detail = store.get_resource_detail(
                "vm-1",
                history_points=10,
                metric="cpu",
                start_ms=start_ms,
                end_ms=end_ms,
            )

            self.assertEqual(len(detail["charts"]["cpu"]["y_train"]), 2)
            self.assertEqual(len(detail["charts"]["cpu"]["y_test"]), 2)

    def test_chart_detail_rejects_reversed_time_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_artifacts(base)
            store = _SingleForecastStore(AppConfig(out_dir=str(base)), GenerationConfig())

            with self.assertRaisesRegex(ValueError, "start_ms"):
                store.get_resource_detail("vm-1", start_ms=2, end_ms=1)

    def test_k8s_chart_request_returns_only_selected_container_and_metric(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = pd.date_range("2026-01-01", periods=8, freq="h")
            values = pd.Series([0.2 + i / 100 for i in range(8)], index=index)
            resource_id = "k8s:cluster:prod:deployment:api"
            metrics = ("cpu_limit", "cpu_request", "memory_limit", "memory_request")
            raw = {
                "resource_id": resource_id,
                "resource_type": "k8s_workload",
                "spec": {
                    "cluster": "cluster",
                    "namespace": "prod",
                    "workload_kind": "deployment",
                    "workload_name": "api",
                    "containers": {"app": {}, "sidecar": {}},
                },
                **{metric: values for metric in metrics},
                "container_metrics": {
                    "app": {metric: values for metric in metrics},
                    "sidecar": {metric: values * 0.5 for metric in metrics},
                },
            }
            block = {
                "preds": {"rolling_mean": [0.2, 0.2]},
                "x_pred_ms": [1, 2],
                "preds_future": {"rolling_mean": [0.2]},
                "metrics": {"rolling_mean": {"rmse": 0.1}},
                "best_method": "rolling_mean",
            }
            detail = {
                "resource_id": resource_id,
                "resource_type": "k8s_workload",
                "charts_forecast": {metric: block for metric in metrics},
                "container_charts_forecast": {
                    container: {metric: block for metric in metrics}
                    for container in ("app", "sidecar")
                },
            }
            write_raw_resource_dataset(base, [raw], freq="h")
            (base / "details").mkdir(parents=True)
            (base / "details" / "part-00000.json").write_text(
                json.dumps({"resources": [detail]}),
                encoding="utf-8",
            )
            (base / "summary_index.json").write_text(
                json.dumps({
                    "meta": {"test_size": 2},
                    "resources": [{
                        "resource_id": resource_id,
                        "detail_ref": {"file": "part-00000.json", "offset": 0},
                    }],
                }),
                encoding="utf-8",
            )
            store = _SingleForecastStore(AppConfig(out_dir=str(base)), GenerationConfig())

            charts = store.get_resource_charts(
                resource_id,
                metric="cpu_limit",
                container="sidecar",
                history_points=3,
            )

            self.assertEqual(set(charts["charts"]), {"cpu_limit"})
            self.assertEqual(set(charts["container_charts"]), {"sidecar"})
            self.assertEqual(set(charts["container_charts"]["sidecar"]), {"cpu_limit"})
            self.assertEqual(len(charts["container_charts"]["sidecar"]["cpu_limit"]["y_train"]), 3)

    def test_partial_prediction_reads_only_selected_raw_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            external = [
                prepared_dict_to_raw_record(_resource("vm-1", 0.1)),
                prepared_dict_to_raw_record(_resource("vm-2", 0.2)),
            ]
            forecast_cfg = {"enabled_methods": ["rolling_mean"], "enable_ensemble": False}
            with patch("resource_predict.pipeline.run.read_forecast_config", return_value=forecast_cfg):
                generate_forecasts(
                    out_dir=str(base),
                    data_provider=lambda resources, n, freq: external,
                    test_size=2,
                    future_steps=1,
                    max_workers=1,
                )
                other_ref = RawResourceStore(base).raw_ref("vm-2")
                other_path = base / Path(*other_ref["file"].split("/"))
                other_path.write_text("{broken", encoding="utf-8")

                outputs = generate_predictions_only(
                    out_dir=str(base),
                    resource_ids=["vm-1"],
                    test_size=2,
                    future_steps=1,
                    max_workers=1,
                )

            self.assertEqual({item["resource_id"] for item in outputs}, {"vm-1", "vm-2"})


if __name__ == "__main__":
    unittest.main()
