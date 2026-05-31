from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resource_predict.data.io import read_raw_dataset
from resource_predict.data.updater import run_upsert_with_data
from resource_predict.pipeline.constants import FORECAST_ERROR_REPORT_FILENAME
from resource_predict.pipeline.run import generate_forecasts
from resource_predict.pipeline.windowing import infer_series_freq, resolve_forecast_window
from resource_predict.settings import settings


def series(points: int, freq: str) -> pd.Series:
    return pd.Series(
        [0.2] * points,
        index=pd.date_range("2026-01-01", periods=points, freq=freq),
    )


class ForecastWindowingTest(unittest.TestCase):
    def test_vm_uses_legacy_point_counts_by_default(self):
        item = {
            "resource_id": "vm-001",
            "resource_type": "openstack_vm",
            "cpu": series(240, "h"),
        }

        window = resolve_forecast_window(
            cfg=settings.generation,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
        )

        self.assertEqual(window.resource_family, "vm")
        self.assertEqual(window.test_size, 72)
        self.assertEqual(window.future_steps, 24)
        self.assertIsNone(window.test_duration)
        self.assertIsNone(window.future_duration)

    def test_workload_duration_is_converted_from_sample_interval(self):
        item = {
            "resource_id": "k8s:cluster-a:ns:deployment:api",
            "resource_type": "k8s_workload",
            "cpu": series(2017, "5min"),
        }

        window = resolve_forecast_window(
            cfg=settings.generation,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
        )

        self.assertEqual(window.resource_family, "workload")
        self.assertEqual(window.sample_interval_seconds, 300.0)
        self.assertEqual(window.test_duration, "24h")
        self.assertEqual(window.future_duration, "24h")
        self.assertEqual(window.test_size, 288)
        self.assertEqual(window.future_steps, 288)

    def test_scoped_point_count_overrides_are_supported(self):
        cfg = replace(
            settings.generation,
            workload_test_duration=None,
            workload_future_duration=None,
            workload_test_size=96,
            workload_future_steps=48,
        )
        item = {
            "resource_id": "k8s:cluster-a:ns:deployment:api",
            "resource_type": "k8s_workload",
            "cpu": series(500, "15min"),
        }

        window = resolve_forecast_window(
            cfg=cfg,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
        )

        self.assertEqual(window.test_size, 96)
        self.assertEqual(window.future_steps, 48)

    def test_default_point_counts_are_explicit_fallbacks(self):
        cfg = replace(
            settings.generation,
            default_test_size=12,
            default_future_steps=6,
            vm_test_duration=None,
            vm_future_duration=None,
            vm_test_size=None,
            vm_future_steps=None,
        )
        item = {
            "resource_id": "vm-001",
            "resource_type": "openstack_vm",
            "cpu": series(40, "h"),
        }

        window = resolve_forecast_window(
            cfg=cfg,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
        )

        self.assertEqual(window.test_size, 12)
        self.assertEqual(window.future_steps, 6)
        self.assertEqual(window.source, "default_test_size,default_future_steps")

    def test_infer_series_freq_handles_two_point_five_minute_series(self):
        idx = pd.date_range("2026-01-01", periods=2, freq="5min")

        self.assertEqual(infer_series_freq(idx), "5min")

    def test_k8s_upsert_writes_inferred_raw_frequency(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            idx = pd.date_range("2026-01-01", periods=300, freq="5min")
            timestamps = (idx.view("int64") // 1_000_000).tolist()
            k8s_item = {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {
                    "cpu": {"timestamps": timestamps, "values": [0.2] * len(timestamps)},
                    "memory": {"timestamps": timestamps, "values": [0.3] * len(timestamps)},
                },
                "spec": {
                    "cluster": "cluster-a",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
                    "pods_observed": ["api-a"],
                    "containers_observed": ["app"],
                    "replicas_observed": 1,
                },
            }

            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[{"resource_id": k8s_item["resource_id"]}]):
                result = run_upsert_with_data([k8s_item], out_dir=base, fail_if_busy=True)

            self.assertTrue(result["success"], result)
            _prepared, meta = read_raw_dataset(base / "raw_data.json")
            self.assertEqual(meta.get("freq"), "5min")

    def test_generate_forecasts_writes_effective_workload_window_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            idx = pd.date_range("2026-01-01", periods=400, freq="5min")
            timestamps = (idx.view("int64") // 1_000_000).tolist()
            item = {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {
                    "cpu": {"timestamps": timestamps, "values": [0.2] * len(timestamps)},
                    "memory": {"timestamps": timestamps, "values": [0.3] * len(timestamps)},
                },
                "spec": {
                    "cluster": "cluster-a",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
                    "pods_observed": ["api-a"],
                    "containers_observed": ["app"],
                    "replicas_observed": 1,
                },
            }

            with patch(
                "resource_predict.pipeline.run.read_forecast_config",
                return_value={"enabled_methods": ["rolling_mean"], "enable_ensemble": False},
            ):
                generate_forecasts(
                    out_dir=str(base),
                    data_provider=lambda resources, n, freq: [item],
                    save_raw=True,
                )

            _prepared, meta = read_raw_dataset(base / "raw_data.json")
            self.assertEqual(meta.get("freq"), "5min")
            stats = __import__("json").loads((base / "generation_stats.json").read_text(encoding="utf-8"))
            self.assertEqual(stats["test_size"], 288)
            self.assertEqual(stats["future_steps"], 288)
            self.assertEqual(stats["forecast_window"]["resource_family"], "workload")
            self.assertEqual(stats["forecast_window"]["test_duration"], "24h")
            self.assertEqual(stats["forecast_error_report_file"], FORECAST_ERROR_REPORT_FILENAME)
            report = __import__("json").loads((base / FORECAST_ERROR_REPORT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(report["meta"]["window"]["test_size"], 288)
            self.assertEqual(report["meta"]["window"]["future_steps"], 288)
            self.assertTrue(report["rows"])
            row = report["rows"][0]
            self.assertEqual(row["resource_id"], item["resource_id"])
            self.assertIn(row["metric"], {"cpu", "memory"})
            self.assertEqual(row["model"], "rolling_mean")
            for key in ("rmse", "mae", "mape", "p95_error"):
                self.assertIn(key, row)
                self.assertIsInstance(row[key], (int, float))
            self.assertEqual(row["window"]["source"], "workload_test_duration,workload_future_duration")


if __name__ == "__main__":
    unittest.main()
