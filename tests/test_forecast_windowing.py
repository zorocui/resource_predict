from __future__ import annotations

from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from resource_predict.data.updater import run_upsert_with_data
from resource_predict.pipeline.constants import FORECAST_ERROR_REPORT_FILENAME, MANIFEST_FILENAME
from resource_predict.pipeline.partial import load_existing_forecast_items
from resource_predict.pipeline.run import generate_forecasts
from resource_predict.pipeline.write_outputs import write_prediction_outputs
from resource_predict.pipeline.prepare import prepare_recent_contiguous_forecast_data
from resource_predict.pipeline.windowing import (
    infer_series_freq,
    recent_contiguous_segment,
    resolve_forecast_window,
)
from resource_predict.settings import settings


K8S_METRICS = ("cpu_limit", "cpu_request", "memory_limit", "memory_request")


def series(points: int, freq: str) -> pd.Series:
    return pd.Series(
        [0.2] * points,
        index=pd.date_range("2026-01-01", periods=points, freq=freq),
    )


class ForecastWindowingTest(unittest.TestCase):
    def test_vm_uses_runtime_duration_by_default(self):
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
        self.assertEqual(window.test_duration, "72h")
        self.assertEqual(window.future_duration, "24h")

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

    def test_single_point_workload_uses_raw_frequency_fallback(self):
        item = {
            "resource_id": "k8s:cluster-a:ns:deployment:api",
            "resource_type": "k8s_workload",
            "cpu": series(1, "5min"),
        }

        window = resolve_forecast_window(
            cfg=settings.generation,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
            fallback_freq="300s",
        )

        self.assertEqual(window.sample_interval_seconds, 300.0)
        self.assertEqual(window.test_size, 288)
        self.assertEqual(window.future_steps, 288)

    def test_single_point_without_fallback_keeps_frequency_unknown_error(self):
        item = {
            "resource_id": "k8s:cluster-a:ns:deployment:api",
            "resource_type": "k8s_workload",
            "cpu": series(1, "5min"),
        }

        with self.assertRaisesRegex(ValueError, "时间序列频率未知"):
            resolve_forecast_window(
                cfg=settings.generation,
                items=[item],
                explicit_test_size=None,
                explicit_future_steps=None,
            )

    def test_actual_interval_wins_over_frequency_fallback(self):
        item = {
            "resource_id": "k8s:cluster-a:ns:deployment:api",
            "resource_type": "k8s_workload",
            "cpu": series(100, "15min"),
        }

        window = resolve_forecast_window(
            cfg=settings.generation,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
            fallback_freq="300s",
        )

        self.assertEqual(window.sample_interval_seconds, 900.0)
        self.assertEqual(window.test_size, 96)
        self.assertEqual(window.future_steps, 96)

    def test_sparse_workload_prefers_configured_frequency(self):
        idx = pd.to_datetime([
            "2026-08-01 00:00", "2026-08-01 00:05", "2026-08-03 00:00"
        ])
        item = {
            "resource_id": "k8s:a:ns:deployment:api",
            "resource_type": "k8s_workload",
            "cpu_limit": pd.Series([0.1, 0.2, 0.3], index=idx),
        }

        window = resolve_forecast_window(
            cfg=settings.generation,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
            fallback_freq="300s",
            prefer_fallback_freq=True,
        )

        self.assertEqual(window.sample_interval_seconds, 300.0)
        self.assertEqual(window.test_size, 288)
        self.assertEqual(window.future_steps, 288)

    def test_vm_observed_interval_still_wins_when_fallback_is_not_preferred(self):
        item = {
            "resource_id": "vm-001",
            "resource_type": "openstack_vm",
            "cpu": series(100, "15min"),
        }

        window = resolve_forecast_window(
            cfg=settings.generation,
            items=[item],
            explicit_test_size=None,
            explicit_future_steps=None,
            fallback_freq="300s",
            prefer_fallback_freq=False,
        )

        self.assertEqual(window.sample_interval_seconds, 900.0)

    def test_recent_contiguous_segment_starts_after_last_large_gap(self):
        idx = pd.to_datetime([
            "2026-08-01 00:00", "2026-08-01 00:05",
            "2026-08-03 00:00", "2026-08-03 00:05", "2026-08-03 00:10",
        ])
        source = pd.Series(range(5), index=idx, dtype=float)

        result = recent_contiguous_segment(source, 300.0, max_gap_steps=3)

        self.assertEqual(result.index[0], pd.Timestamp("2026-08-03 00:00"))
        self.assertEqual(result.tolist(), [2.0, 3.0, 4.0])

    def test_workload_and_container_metrics_use_independent_recent_segments(self):
        old = pd.date_range("2026-08-01", periods=2, freq="5min")
        top_recent = pd.date_range("2026-08-03", periods=4, freq="5min")
        container_recent = pd.date_range("2026-08-04", periods=3, freq="5min")
        top = pd.Series(range(6), index=old.append(top_recent), dtype=float)
        container = pd.Series(range(5), index=old.append(container_recent), dtype=float)
        item = {
            "resource_id": "k8s:a:ns:deployment:api",
            "resource_type": "k8s_workload",
            **{metric: top for metric in K8S_METRICS},
            "container_metrics": {
                "app": {metric: container for metric in K8S_METRICS}
            },
            "data_quality": {metric: {"level": "poor"} for metric in K8S_METRICS},
            "container_data_quality": {
                "app": {metric: {"level": "poor"} for metric in K8S_METRICS}
            },
        }

        trimmed, skips = prepare_recent_contiguous_forecast_data(
            [item],
            sample_interval_seconds=300.0,
            max_gap_steps=3,
            test_size=3,
        )

        self.assertEqual(len(item["cpu_limit"]), 6)
        self.assertEqual(trimmed[0]["cpu_limit"].index[0], top_recent[0])
        self.assertEqual(trimmed[0]["container_metrics"]["app"]["cpu_limit"].index[0], container_recent[0])
        quality = trimmed[0]["data_quality"]["cpu_limit"]
        self.assertEqual(quality["recent_contiguous_points"], 4)
        self.assertEqual(quality["data_end_ms"], int(top.index.max().value // 1_000_000))
        self.assertFalse(quality["prediction_skipped"])
        container_quality = trimmed[0]["container_data_quality"]["app"]["cpu_limit"]
        self.assertTrue(container_quality["prediction_skipped"])
        self.assertIn(
            {
                "resource_id": item["resource_id"],
                "metric": "container/app/cpu_limit",
                "reason": "recent_contiguous_segment_too_short",
            },
            skips,
        )

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
                "metrics": {metric: {"timestamps": timestamps, "values": [0.2] * len(timestamps)} for metric in K8S_METRICS},
                "spec": {
                    "cluster": "cluster-a",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
                    "pods_observed": ["api-a"],
                    "containers_observed": ["app"],
                    "containers": {
                        "app": {
                            "cpu_request_cores": 0.5,
                            "cpu_limit_cores": 1.0,
                            "memory_request_gb": 0.5,
                            "memory_limit_gb": 1.0,
                        }
                    },
                    "replicas_observed": 1,
                },
            }

            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[{"resource_id": k8s_item["resource_id"]}]):
                result = run_upsert_with_data([k8s_item], out_dir=base, fail_if_busy=True)

            self.assertTrue(result["success"], result)
            meta = RawResourceStore(base).metadata()
            self.assertEqual(meta.get("freq"), "5min")

    def test_single_point_k8s_upsert_preserves_frequency_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            timestamp = int(pd.Timestamp("2026-01-01").timestamp() * 1000)
            k8s_item = {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {
                    metric: {"timestamps": [timestamp], "values": [0.2]}
                    for metric in K8S_METRICS
                },
                "spec": {
                    "cluster": "cluster-a",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
                    "containers": {},
                },
            }

            result = run_upsert_with_data(
                [k8s_item],
                out_dir=base,
                fail_if_busy=True,
                freq_hint="300s",
            )

            self.assertTrue(result["success"], result)
            self.assertEqual(RawResourceStore(base).metadata().get("freq"), "300s")

    def test_k8s_upsert_merges_container_metrics_for_existing_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            idx = pd.date_range("2026-01-01", periods=300, freq="5min")
            old_values = [0.2] * len(idx)
            existing = {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "spec": {
                    "cluster": "cluster-a",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
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
                **{metric: pd.Series(old_values, index=idx) for metric in K8S_METRICS},
                "container_metrics": {
                    "app": {
                        metric: pd.Series(old_values, index=idx)
                        for metric in K8S_METRICS
                    }
                },
            }
            write_raw_resource_dataset(base, [existing], freq="5min")

            new_idx = pd.date_range(idx[-1] + pd.Timedelta(minutes=5), periods=2, freq="5min")
            timestamps = (new_idx.view("int64") // 1_000_000).tolist()
            incoming = {
                "resource_id": existing["resource_id"],
                "resource_type": "k8s_workload",
                "metrics": {
                    metric: {"timestamps": timestamps, "values": [0.3, 0.4]}
                    for metric in K8S_METRICS
                },
                "spec": existing["spec"],
                "container_metrics": {
                    "app": {
                        metric: {"timestamps": timestamps, "values": [0.6, 0.8]}
                        for metric in K8S_METRICS
                    }
                },
                "container_data_quality": {"app": {"cpu_limit": {"level": "good"}}},
                "container_metric_modes": {"app": {"cpu_limit": "cpu_usage/cpu_limit"}},
            }

            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[{"resource_id": existing["resource_id"]}]):
                result = run_upsert_with_data([incoming], out_dir=base, fail_if_busy=True)

            self.assertTrue(result["success"], result)
            prepared = RawResourceStore(base).read_many()
            loaded = prepared[0]
            self.assertAlmostEqual(loaded["container_metrics"]["app"]["cpu_limit"].iloc[-1], 0.8)
            self.assertEqual(len(loaded["container_metrics"]["app"]["cpu_limit"]), len(idx) + 2)
            self.assertEqual(loaded["container_data_quality"]["app"]["cpu_limit"]["level"], "good")
            self.assertEqual(loaded["container_metric_modes"]["app"]["cpu_limit"], "cpu_usage/cpu_limit")

    def test_k8s_upsert_patches_container_specs_without_dropping_untouched_containers(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            idx = pd.date_range("2026-01-01", periods=300, freq="5min")
            existing = {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "spec": {
                    "cluster": "cluster-a",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
                    "containers_observed": ["app", "sidecar"],
                    "containers": {
                        "app": {
                            "cpu_request_cores": 0.5,
                            "cpu_limit_cores": 1.0,
                            "memory_request_gb": 0.5,
                            "memory_limit_gb": 1.0,
                        },
                        "sidecar": {
                            "cpu_request_cores": 0.1,
                            "cpu_limit_cores": 0.2,
                            "memory_request_gb": 0.1,
                            "memory_limit_gb": 0.2,
                        },
                    },
                },
                **{metric: pd.Series([0.2] * len(idx), index=idx) for metric in K8S_METRICS},
            }
            write_raw_resource_dataset(base, [existing], freq="5min")

            new_idx = pd.date_range(idx[-1] + pd.Timedelta(minutes=5), periods=2, freq="5min")
            timestamps = (new_idx.view("int64") // 1_000_000).tolist()
            incoming = {
                "resource_id": existing["resource_id"],
                "resource_type": "k8s_workload",
                "metrics": {
                    metric: {"timestamps": timestamps, "values": [0.3, 0.4]}
                    for metric in K8S_METRICS
                },
                "spec": {
                    "containers": {
                        "app": {
                            "cpu_request_cores": 0.8,
                            "cpu_limit_cores": None,
                        }
                    }
                },
            }

            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[{"resource_id": existing["resource_id"]}]):
                result = run_upsert_with_data([incoming], out_dir=base, fail_if_busy=True)

            self.assertTrue(result["success"], result)
            prepared = RawResourceStore(base).read_many()
            containers = prepared[0]["spec"]["containers"]
            self.assertEqual(containers["app"]["cpu_request_cores"], 0.8)
            self.assertEqual(containers["app"]["cpu_limit_cores"], 1.0)
            self.assertEqual(containers["sidecar"]["cpu_request_cores"], 0.1)
            self.assertEqual(containers["sidecar"]["memory_limit_gb"], 0.2)

    def test_generate_forecasts_writes_effective_workload_window_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            idx = pd.date_range("2026-01-01", periods=400, freq="5min")
            timestamps = (idx.view("int64") // 1_000_000).tolist()
            item = {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {metric: {"timestamps": timestamps, "values": [0.2] * len(timestamps)} for metric in K8S_METRICS},
                "spec": {
                    "cluster": "cluster-a",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
                    "pods_observed": ["api-a"],
                    "containers_observed": ["app"],
                    "containers": {
                        "app": {
                            "cpu_request_cores": 0.5,
                            "cpu_limit_cores": 1.0,
                            "memory_request_gb": 0.5,
                            "memory_limit_gb": 1.0,
                        }
                    },
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

            meta = RawResourceStore(base).metadata()
            self.assertEqual(meta.get("freq"), "10min")
            stats = __import__("json").loads((base / "generation_stats.json").read_text(encoding="utf-8"))
            self.assertEqual(stats["test_size"], 144)
            self.assertEqual(stats["future_steps"], 144)
            self.assertEqual(stats["forecast_window"]["resource_family"], "workload")
            self.assertEqual(stats["forecast_window"]["test_duration"], "24h")
            self.assertEqual(stats["forecast_error_report_file"], FORECAST_ERROR_REPORT_FILENAME)
            report = __import__("json").loads((base / FORECAST_ERROR_REPORT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(report["meta"]["window"]["test_size"], 144)
            self.assertEqual(report["meta"]["window"]["future_steps"], 144)
            self.assertTrue(report["rows"])
            row = report["rows"][0]
            self.assertEqual(row["resource_id"], item["resource_id"])
            self.assertIn(row["metric"], set(K8S_METRICS))
            self.assertEqual(row["model"], "rolling_mean")
            for key in ("rmse", "mae", "mape", "p95_error"):
                self.assertIn(key, row)
                self.assertIsInstance(row[key], (int, float))
            self.assertEqual(row["window"]["source"], "workload_test_duration,workload_future_duration")

    def test_short_recent_segment_retains_old_prediction_and_records_skip_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rid = "k8s:cluster-a:ns:deployment:api"
            old_item = {
                "resource_id": rid,
                "resource_type": "k8s_workload",
                "spec": {"cluster": "cluster-a", "namespace": "ns", "containers": {}},
                "best_methods": {},
                "metrics": {},
                "charts_forecast": {},
                "observed_stats": {"marker": "retained"},
            }
            write_prediction_outputs(
                out_base=base,
                resources_items=[old_item],
                active_methods=["rolling_mean"],
                test_size=3,
                future_steps=2,
                forecast_window={"sample_interval_seconds": 300.0},
                detail_chunk_size=100,
                predicted_count=1,
                partial_resource_ids=set(),
                metric_filter_by_id={},
                metric_partial_enabled=False,
                total_elapsed=0.0,
                raw_stats={},
            )
            idx = pd.to_datetime([
                "2026-08-01 00:00", "2026-08-01 00:05",
                "2026-08-03 00:00", "2026-08-03 00:05", "2026-08-03 00:10",
            ])
            timestamps = (idx.view("int64") // 1_000_000).tolist()
            incoming = {
                "resource_id": rid,
                "resource_type": "k8s_workload",
                "metrics": {
                    metric: {"timestamps": timestamps, "values": [0.2] * len(idx)}
                    for metric in K8S_METRICS
                },
                "spec": old_item["spec"],
            }

            generate_forecasts(
                out_dir=str(base),
                data_provider=lambda resources, n, freq: [incoming],
                test_size=3,
                future_steps=2,
                freq="300s",
                save_raw=True,
            )

            raw = RawResourceStore(base).get(rid)
            self.assertEqual(len(raw["cpu_limit"]), 5)
            retained = load_existing_forecast_items(base)[0]
            self.assertEqual(retained["observed_stats"]["marker"], "retained")
            self.assertEqual(retained["data_quality"]["cpu_limit"]["recent_contiguous_points"], 3)
            self.assertTrue(retained["data_quality"]["cpu_limit"]["prediction_skipped"])
            manifest = json.loads((base / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            report = json.loads((base / FORECAST_ERROR_REPORT_FILENAME).read_text(encoding="utf-8"))
            expected = {
                "resource_id": rid,
                "metric": "cpu_limit",
                "reason": "recent_contiguous_segment_too_short",
            }
            self.assertIn(expected, manifest["meta"]["prediction_skips"])
            self.assertIn(expected, report["meta"]["prediction_skips"])

    def test_short_container_segment_retains_its_previous_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rid = "k8s:cluster-a:ns:deployment:api"
            old_item = {
                "resource_id": rid,
                "resource_type": "k8s_workload",
                "spec": {"cluster": "cluster-a", "namespace": "ns", "containers": {"app": {}}},
                "best_methods": {},
                "metrics": {},
                "charts_forecast": {},
                "observed_stats": {},
                "container_charts_forecast": {
                    "app": {"cpu_limit": {"best_method": "retained-model"}}
                },
            }
            write_prediction_outputs(
                out_base=base,
                resources_items=[old_item],
                active_methods=["rolling_mean"],
                test_size=3,
                future_steps=2,
                forecast_window={"sample_interval_seconds": 300.0},
                detail_chunk_size=100,
                predicted_count=1,
                partial_resource_ids=set(),
                metric_filter_by_id={},
                metric_partial_enabled=False,
                total_elapsed=0.0,
                raw_stats={},
            )
            top_idx = pd.date_range("2026-08-03", periods=5, freq="5min")
            container_idx = pd.to_datetime([
                "2026-08-01 00:00", "2026-08-01 00:05",
                "2026-08-03 00:00", "2026-08-03 00:05", "2026-08-03 00:10",
            ])
            incoming = {
                "resource_id": rid,
                "resource_type": "k8s_workload",
                "metrics": {
                    metric: {
                        "timestamps": (top_idx.view("int64") // 1_000_000).tolist(),
                        "values": [0.2] * len(top_idx),
                    }
                    for metric in K8S_METRICS
                },
                "container_metrics": {
                    "app": {
                        metric: {
                            "timestamps": (container_idx.view("int64") // 1_000_000).tolist(),
                            "values": [0.2] * len(container_idx),
                        }
                        for metric in K8S_METRICS
                    }
                },
                "spec": old_item["spec"],
            }

            with patch(
                "resource_predict.pipeline.run.read_forecast_config",
                return_value={"enabled_methods": ["rolling_mean"], "enable_ensemble": False},
            ):
                generate_forecasts(
                    out_dir=str(base),
                    data_provider=lambda resources, n, freq: [incoming],
                    test_size=3,
                    future_steps=2,
                    save_raw=True,
                )

            result = load_existing_forecast_items(base)[0]
            self.assertEqual(
                result["container_charts_forecast"]["app"]["cpu_limit"]["best_method"],
                "retained-model",
            )
            self.assertTrue(
                result["container_data_quality"]["app"]["cpu_limit"]["prediction_skipped"]
            )

    def test_partial_short_segment_keeps_unrelated_existing_forecasts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rid = "k8s:cluster-a:ns:deployment:api"
            other_rid = "k8s:cluster-a:ns:deployment:worker"
            old_items = []
            for resource_id in (rid, other_rid):
                old_items.append({
                    "resource_id": resource_id,
                    "resource_type": "k8s_workload",
                    "spec": {"cluster": "cluster-a", "namespace": "ns", "containers": {}},
                    "best_methods": {},
                    "metrics": {},
                    "charts_forecast": {},
                    "observed_stats": {"marker": resource_id},
                })
            write_prediction_outputs(
                out_base=base,
                resources_items=old_items,
                active_methods=["rolling_mean"],
                test_size=3,
                future_steps=2,
                forecast_window={"sample_interval_seconds": 300.0},
                detail_chunk_size=100,
                predicted_count=2,
                partial_resource_ids=set(),
                metric_filter_by_id={},
                metric_partial_enabled=False,
                total_elapsed=0.0,
                raw_stats={},
            )
            idx = pd.to_datetime([
                "2026-08-01 00:00", "2026-08-01 00:05",
                "2026-08-03 00:00", "2026-08-03 00:05", "2026-08-03 00:10",
            ])
            sparse = {
                "resource_id": rid,
                "resource_type": "k8s_workload",
                "spec": old_items[0]["spec"],
                **{metric: pd.Series([0.2] * len(idx), index=idx) for metric in K8S_METRICS},
            }
            write_raw_resource_dataset(base, [sparse], freq="5min")

            generate_forecasts(
                out_dir=str(base),
                predict_only=True,
                resource_ids=[rid],
                test_size=3,
                future_steps=2,
            )

            result_ids = {
                item["resource_id"] for item in load_existing_forecast_items(base)
            }
            self.assertEqual(result_ids, {rid, other_rid})


if __name__ == "__main__":
    unittest.main()
