from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from resource_predict.data.io import (
    _detect_ts_unit,
    atomic_write_json,
    atomic_write_text,
    coerce_metric_series,
    merge_charts_into_detail,
    read_raw_dataset,
    write_raw_dataset,
)


class CoerceMetricSeriesTest(unittest.TestCase):
    """Tests for coerce_metric_series."""

    def test_from_dict_with_ms_timestamps(self):
        """Dict with millisecond-epoch timestamps is converted to a DatetimeIndex Series."""
        now_ms = 1_700_000_000_000  # ~2023 in ms
        data = {
            "timestamps": [now_ms, now_ms + 60_000, now_ms + 120_000],
            "values": [10.0, 20.0, 30.0],
        }
        s = coerce_metric_series(data, "cpu")
        self.assertIsInstance(s.index, pd.DatetimeIndex)
        self.assertEqual(len(s), 3)
        self.assertEqual(s.name, "cpu")
        self.assertAlmostEqual(s.iloc[0], 10.0)
        self.assertAlmostEqual(s.iloc[-1], 30.0)

    def test_from_dict_with_s_timestamps(self):
        """Dict with second-epoch timestamps is correctly parsed."""
        now_s = 1_700_000_000  # ~2023 in seconds
        data = {
            "timestamps": [now_s, now_s + 60, now_s + 120],
            "values": [5.0, 15.0, 25.0],
        }
        s = coerce_metric_series(data, "memory")
        self.assertIsInstance(s.index, pd.DatetimeIndex)
        self.assertEqual(len(s), 3)
        self.assertAlmostEqual(s.iloc[1], 15.0)

    def test_from_series_passthrough(self):
        """An existing pd.Series with DatetimeIndex is preserved (copy)."""
        idx = pd.date_range("2024-01-01", periods=4, freq="h")
        original = pd.Series([1.0, 2.0, 3.0, 4.0], index=idx, name="cpu")
        result = coerce_metric_series(original, "cpu")
        self.assertEqual(len(result), 4)
        self.assertTrue(result.index.equals(original.index))
        # Must be a copy, not the same object
        self.assertIsNot(result, original)

    def test_error_on_missing_fields(self):
        """Dict missing 'timestamps' or 'values' raises ValueError."""
        with self.assertRaises(ValueError):
            coerce_metric_series({"timestamps": [1, 2]}, "cpu")
        with self.assertRaises(ValueError):
            coerce_metric_series({"values": [1, 2]}, "cpu")

    def test_error_on_unsupported_type(self):
        """Non-dict, non-Series input raises TypeError."""
        with self.assertRaises(TypeError):
            coerce_metric_series([1, 2, 3], "cpu")


class DetectTsUnitTest(unittest.TestCase):
    """Tests for _detect_ts_unit."""

    def test_seconds(self):
        self.assertEqual(_detect_ts_unit([1_700_000_000]), "s")

    def test_milliseconds(self):
        self.assertEqual(_detect_ts_unit([1_700_000_000_000]), "ms")

    def test_string_returns_none(self):
        self.assertIsNone(_detect_ts_unit(["2024-01-01T00:00:00"]))

    def test_empty_returns_none(self):
        self.assertIsNone(_detect_ts_unit([]))


class AtomicWriteTest(unittest.TestCase):
    """Tests for atomic_write_text and atomic_write_json."""

    def test_atomic_write_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            atomic_write_text(path, "hello world")
            self.assertEqual(path.read_text(encoding="utf-8"), "hello world")

    def test_atomic_write_text_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "dir" / "out.txt"
            atomic_write_text(path, "nested")
            self.assertEqual(path.read_text(encoding="utf-8"), "nested")

    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            payload = {"key": "value", "num": 42}
            atomic_write_json(path, payload)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded, payload)


class RawDatasetRoundtripTest(unittest.TestCase):
    """Tests for write_raw_dataset + read_raw_dataset."""

    def _make_prepared_resource(self, resource_id="vm-01", n=20):
        idx = pd.date_range("2024-01-01", periods=n, freq="h")
        return {
            "resource_id": resource_id,
            "resource_type": "openstack_vm",
            "spec": {"flavor": "m1.small"},
            "cpu": pd.Series(np.linspace(0.1, 0.5, n), index=idx, name="cpu"),
            "memory": pd.Series(np.linspace(0.2, 0.6, n), index=idx, name="memory"),
            "disk": pd.Series(np.linspace(0.05, 0.3, n), index=idx, name="disk"),
        }

    def test_write_then_read_roundtrip(self):
        res = self._make_prepared_resource()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw_data.json"
            write_raw_dataset(path, [res], freq="h")

            prepared_list, meta = read_raw_dataset(path)

        self.assertEqual(meta["freq"], "h")
        self.assertEqual(len(prepared_list), 1)
        loaded = prepared_list[0]
        self.assertEqual(loaded["resource_id"], "vm-01")
        self.assertIsInstance(loaded["cpu"], pd.Series)
        self.assertEqual(len(loaded["cpu"]), 20)
        # Metric values should survive the roundtrip approximately
        self.assertAlmostEqual(loaded["cpu"].iloc[0], 0.1, places=4)

    def test_write_then_read_k8s_container_metrics(self):
        idx = pd.date_range("2024-01-01", periods=12, freq="h")
        series = pd.Series(np.linspace(0.1, 0.6, 12), index=idx)
        res = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "containers_observed": ["app"],
                "containers": {
                    "app": {
                        "cpu_request_cores": 0.5,
                        "cpu_limit_cores": 1.0,
                        "memory_request_gb": 0.5,
                        "memory_limit_gb": 1.0,
                    }
                },
            },
            "cpu_limit": series,
            "cpu_request": series,
            "memory_limit": series,
            "memory_request": series,
            "container_metrics": {
                "app": {
                    "cpu_limit": series,
                    "cpu_request": series,
                    "memory_limit": series,
                    "memory_request": series,
                }
            },
            "container_data_quality": {"app": {"cpu_limit": {"level": "good"}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw_data.json"
            write_raw_dataset(path, [res], freq="h")
            prepared_list, _meta = read_raw_dataset(path)

        loaded = prepared_list[0]
        self.assertIn("container_metrics", loaded)
        self.assertIsInstance(loaded["container_metrics"]["app"]["cpu_limit"], pd.Series)
        self.assertAlmostEqual(loaded["container_metrics"]["app"]["cpu_limit"].iloc[-1], 0.6)
        self.assertEqual(loaded["container_data_quality"]["app"]["cpu_limit"]["level"], "good")

    def test_read_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nonexistent.json"
            with self.assertRaises(FileNotFoundError):
                read_raw_dataset(path)


class MergeChartsIntoDetailTest(unittest.TestCase):
    """Tests for merge_charts_into_detail."""

    def _make_raw_series(self, n=30):
        idx = pd.date_range("2024-01-01", periods=n, freq="h")
        return pd.Series(np.linspace(0.1, 0.9, n), index=idx, name="cpu")

    def test_basic_merge(self):
        """Merges forecast charts with raw series to produce full charts."""
        n = 30
        test_size = 5
        cpu_series = self._make_raw_series(n)
        raw = {
            "resource_id": "vm-01",
            "resource_type": "openstack_vm",
            "spec": {"flavor": "m1.small"},
            "cpu": cpu_series,
            "memory": self._make_raw_series(n),
            "disk": self._make_raw_series(n),
        }
        detail = {
            "resource_id": "vm-01",
            "charts_forecast": {
                "cpu": {
                    "preds": {"arima": [0.5, 0.6]},
                    "x_pred_ms": [1, 2],
                    "preds_future": {},
                    "metrics": {},
                    "best_method": "arima",
                },
            },
        }
        raw_by_id = {"vm-01": raw}
        result = merge_charts_into_detail(detail, raw_by_id, test_size=test_size)

        self.assertIn("charts", result)
        self.assertNotIn("charts_forecast", result)
        cpu_chart = result["charts"]["cpu"]
        self.assertEqual(len(cpu_chart["y_train"]), n - test_size)
        self.assertEqual(len(cpu_chart["y_test"]), test_size)
        self.assertEqual(cpu_chart["best_method"], "arima")

    def test_merges_container_charts(self):
        n = 30
        test_size = 5
        series = self._make_raw_series(n)
        raw = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {},
            "cpu_limit": series,
            "cpu_request": series,
            "memory_limit": series,
            "memory_request": series,
            "container_metrics": {
                "app": {
                    "cpu_limit": series,
                    "cpu_request": series,
                    "memory_limit": series,
                    "memory_request": series,
                }
            },
        }
        detail = {
            "resource_id": raw["resource_id"],
            "charts_forecast": {
                "cpu_limit": {
                    "preds": {"arima": [0.5]},
                    "x_pred_ms": [1],
                    "preds_future": {"arima": [0.6]},
                    "metrics": {},
                    "best_method": "arima",
                }
            },
            "container_charts_forecast": {
                "app": {
                    "cpu_limit": {
                        "preds": {"arima": [0.5]},
                        "x_pred_ms": [1],
                        "preds_future": {"arima": [0.6]},
                        "metrics": {},
                        "best_method": "arima",
                    }
                }
            },
        }
        result = merge_charts_into_detail(detail, {raw["resource_id"]: raw}, test_size=test_size)

        self.assertIn("container_charts", result)
        app_cpu = result["container_charts"]["app"]["cpu_limit"]
        self.assertEqual(len(app_cpu["y_train"]), n - test_size)
        self.assertEqual(len(app_cpu["y_test"]), test_size)
        self.assertEqual(app_cpu["best_method"], "arima")
        self.assertNotIn("container_charts_forecast", result)

    def test_skips_if_already_has_y_train(self):
        """Detail already containing y_train is returned unchanged."""
        detail = {
            "resource_id": "vm-01",
            "charts": {
                "cpu": {
                    "y_train": [0.1, 0.2, 0.3],
                    "x_train_ms": [1, 2, 3],
                },
            },
        }
        result = merge_charts_into_detail(detail, {}, test_size=5)
        # Should be the exact same dict, not modified
        self.assertEqual(result["charts"]["cpu"]["y_train"], [0.1, 0.2, 0.3])
        self.assertNotIn("charts_forecast", result)

    def test_returns_unchanged_when_no_raw_found(self):
        """If the resource_id is absent from raw_by_id, detail is returned as-is."""
        detail = {
            "resource_id": "vm-99",
            "charts_forecast": {"cpu": {"preds": {}, "x_pred_ms": []}},
        }
        result = merge_charts_into_detail(detail, {}, test_size=5)
        self.assertIn("charts_forecast", result)
        self.assertNotIn("charts", result)


if __name__ == "__main__":
    unittest.main()
