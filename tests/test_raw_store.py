import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from resource_predict.data import updater
from resource_predict.data.updater import run_update_with_data, run_upsert_with_data
from resource_predict.pipeline.constants import RAW_INDEX_FILENAME
from resource_predict.pipeline.partial import load_existing_forecast_items
from resource_predict.pipeline.write_outputs import write_prediction_outputs


def _series(values, freq="5min"):
    return pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values), freq=freq))


def _vm(resource_id, values=(0.1, 0.2, 0.3)):
    return {
        "resource_id": resource_id,
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
        "cpu": _series(values),
        "memory": _series(values),
        "disk": _series(values),
    }


def _k8s(resource_id):
    values = _series((0.2, 0.3, 0.4))
    return {
        "resource_id": resource_id,
        "resource_type": "k8s_workload",
        "spec": {"cluster": "a", "namespace": "prod", "containers": {"api": {}}},
        "cpu_limit": values,
        "cpu_request": values,
        "memory_limit": values,
        "memory_request": values,
        "container_metrics": {
            "api": {
                "cpu_limit": values,
                "cpu_request": values,
                "memory_limit": values,
                "memory_request": values,
            }
        },
    }


def _forecast_item(resource_id, marker):
    return {
        "resource_id": resource_id,
        "resource_type": "k8s_workload",
        "spec": {"cluster": "a", "namespace": "prod", "containers": {"api": {}}},
        "best_methods": {},
        "metrics": {},
        "charts_forecast": {},
        "observed_stats": {"marker": marker},
    }


def _write_forecast_items(base, items):
    return write_prediction_outputs(
        out_base=base,
        resources_items=items,
        active_methods=["rolling_mean"],
        test_size=1,
        future_steps=1,
        forecast_window={"sample_interval_seconds": 300.0},
        detail_chunk_size=100,
        predicted_count=len(items),
        partial_resource_ids=set(),
        metric_filter_by_id={},
        metric_partial_enabled=False,
        total_elapsed=0.0,
        raw_stats={},
    )


class RawResourceStoreTest(unittest.TestCase):
    def test_trim_series_retains_exact_thirty_day_boundary(self):
        idx = pd.DatetimeIndex([
            "2026-06-30 23:59:59",
            "2026-07-01 00:00:00",
            "2026-07-20 12:00:00",
            "2026-07-31 00:00:00",
        ])
        series = pd.Series([0.1, 0.2, 0.3, 0.4], index=idx)

        trimmed = updater._trim_series_to_retention(series, 30)

        self.assertEqual(trimmed.index.tolist(), idx[1:].tolist())
        self.assertEqual(
            updater._trim_series_to_retention(series.iloc[-1:], 30).tolist(),
            [0.4],
        )
        for invalid in (0, -1, True, 30.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    updater._trim_series_to_retention(series, invalid)

    def test_roundtrip_vm_and_k8s_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stats = write_raw_resource_dataset(
                base,
                [_vm("vm-1"), _k8s("k8s:a:prod:deployment:api")],
                freq="5min",
            )
            store = RawResourceStore(base)

            vm = store.get("vm-1")
            workload = store.get("k8s:a:prod:deployment:api")

            self.assertEqual(stats["resources"], 2)
            self.assertEqual(vm["cpu"].tolist(), [0.1, 0.2, 0.3])
            self.assertIn("api", workload["container_metrics"])
            self.assertEqual(store.metadata()["freq"], "5min")

    def test_single_resource_read_does_not_open_other_resource_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1"), _vm("vm-2")], freq="5min")
            index = json.loads((base / RAW_INDEX_FILENAME).read_text(encoding="utf-8"))
            other = base / Path(*index["resources"]["vm-2"]["file"].split("/"))
            other.write_text("{broken", encoding="utf-8")

            loaded = RawResourceStore(base).get("vm-1")

            self.assertEqual(loaded["resource_id"], "vm-1")

    def test_second_read_hits_single_resource_lru_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1")], freq="5min")
            store = RawResourceStore(base)

            store.get("vm-1")
            self.assertFalse(store.last_cache_hit)
            store.get("vm-1")

            self.assertTrue(store.last_cache_hit)

    def test_partial_update_reuses_unchanged_file_and_replaces_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            original = [_vm("vm-1"), _vm("vm-2")]
            write_raw_resource_dataset(base, original, freq="5min")
            before = json.loads((base / RAW_INDEX_FILENAME).read_text(encoding="utf-8"))
            old_changed_path = base / Path(*before["resources"]["vm-1"]["file"].split("/"))
            updated = [_vm("vm-1", (0.4, 0.5, 0.6)), original[1]]

            stats = write_raw_resource_dataset(
                base,
                updated,
                freq="5min",
                changed_resource_ids={"vm-1"},
            )
            after = json.loads((base / RAW_INDEX_FILENAME).read_text(encoding="utf-8"))

            self.assertNotEqual(before["resources"]["vm-1"]["file"], after["resources"]["vm-1"]["file"])
            self.assertEqual(before["resources"]["vm-2"]["file"], after["resources"]["vm-2"]["file"])
            self.assertEqual(stats["files_written"], 1)
            self.assertGreaterEqual(stats["files_reused"], 1)
            self.assertTrue(old_changed_path.exists(), "旧快照文件应保留安全宽限期")

    def test_full_write_removes_resources_absent_from_new_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1"), _vm("vm-2")], freq="5min")
            write_raw_resource_dataset(base, [_vm("vm-1")], freq="5min")

            store = RawResourceStore(base)

            self.assertEqual(store.resource_ids(), ["vm-1"])
            self.assertIsNone(store.get("vm-2"))

    def test_old_monolithic_file_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "raw_data.json").write_text(
                json.dumps({"meta": {"schema_version": 1}, "resources": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FileNotFoundError, "raw_index.json"):
                RawResourceStore(base).resource_ids()

    def test_tampered_resource_file_fails_content_hash_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1")], freq="5min")
            ref = RawResourceStore(base).raw_ref("vm-1")
            path = base / Path(*ref["file"].split("/"))
            record = json.loads(path.read_text(encoding="utf-8"))
            record["spec"]["cpu_cores"] = 99
            path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "哈希不匹配"):
                RawResourceStore(base).get("vm-1")

    def test_incremental_commits_eventually_remove_expired_orphan_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1")], freq="5min")
            old_ref = RawResourceStore(base).raw_ref("vm-1")
            old_path = base / Path(*old_ref["file"].split("/"))

            write_raw_resource_dataset(
                base,
                [_vm("vm-1", (0.4, 0.5, 0.6))],
                freq="5min",
                changed_resource_ids={"vm-1"},
            )
            expired = time.time() - 301
            os.utime(old_path, (expired, expired))

            write_raw_resource_dataset(
                base,
                [_vm("vm-1", (0.7, 0.8, 0.9))],
                freq="5min",
                changed_resource_ids={"vm-1"},
            )

            self.assertFalse(old_path.exists())

    def test_failed_index_commit_keeps_previous_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1")], freq="5min")

            with patch(
                "resource_predict.data.raw_store.atomic_write_json",
                side_effect=OSError("simulated index commit failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated"):
                    write_raw_resource_dataset(
                        base,
                        [_vm("vm-1", (0.7, 0.8, 0.9))],
                        freq="5min",
                        changed_resource_ids={"vm-1"},
                    )

            loaded = RawResourceStore(base).get("vm-1")
            self.assertEqual(loaded["cpu"].tolist(), [0.1, 0.2, 0.3])

    def test_partial_commit_rejects_changed_id_without_resource_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1"), _vm("vm-2")], freq="5min")

            with self.assertRaisesRegex(ValueError, "vm-2"):
                write_raw_resource_dataset(
                    base,
                    [_vm("vm-1", (0.4, 0.5, 0.6))],
                    freq="5min",
                    changed_resource_ids={"vm-1", "vm-2"},
                )

            self.assertEqual(set(RawResourceStore(base).resource_ids()), {"vm-1", "vm-2"})

    def test_push_update_does_not_read_unrelated_raw_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1"), _vm("vm-2")], freq="5min")
            other_ref = RawResourceStore(base).raw_ref("vm-2")
            other_path = base / Path(*other_ref["file"].split("/"))
            other_path.write_text("{broken", encoding="utf-8")
            timestamp = int(pd.Timestamp("2026-01-01 00:20:00").timestamp() * 1000)

            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[]):
                result = run_update_with_data(
                    [{
                        "resource_id": "vm-1",
                        "metrics": {"cpu": {"timestamps": [timestamp], "values": [0.4]}},
                    }],
                    out_dir=base,
                    fail_if_busy=True,
                )

            self.assertTrue(result["success"], result.get("error"))
            self.assertEqual(RawResourceStore(base).get("vm-1")["cpu"].iloc[-1], 0.4)

    def test_partial_k8s_upsert_preserves_missing_workload_and_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rid_a = "k8s:a:prod:deployment:api"
            rid_b = "k8s:a:prod:deployment:worker"
            resource_a = _k8s(rid_a)
            resource_b = _k8s(rid_b)
            original_values = resource_b["cpu_limit"].tolist()
            write_raw_resource_dataset(base, [resource_a, resource_b], freq="5min")
            _write_forecast_items(
                base,
                [_forecast_item(rid_a, "old-a"), _forecast_item(rid_b, "old-b")],
            )
            timestamp = int(pd.Timestamp("2026-01-01 00:20:00").timestamp() * 1000)

            def fake_worker(index, prepared, **_kwargs):
                self.assertEqual([item["resource_id"] for item in prepared], [rid_a])
                return {**_forecast_item(rid_a, "new-a"), "_slot": index, "_timings": {}}

            window = type("Window", (), {
                "test_size": 1,
                "future_steps": 1,
                "sample_interval_seconds": 300.0,
                "source": "test",
                "resource_family": "k8s",
                "test_duration": "5min",
                "future_duration": "5min",
            })()
            with patch("resource_predict.pipeline.run.resolve_forecast_window", return_value=window):
                with patch("resource_predict.pipeline.run._worker", side_effect=fake_worker):
                    result = run_upsert_with_data(
                        [{
                            "resource_id": rid_a,
                            "resource_type": "k8s_workload",
                            "metrics": {
                                metric: {"timestamps": [timestamp], "values": [0.5]}
                                for metric in (
                                    "cpu_limit", "cpu_request", "memory_limit", "memory_request"
                                )
                            },
                        }],
                        out_dir=base,
                        fail_if_busy=True,
                        freq_hint="5min",
                    )

            self.assertTrue(result["success"], result.get("error"))
            store = RawResourceStore(base)
            self.assertEqual(set(store.resource_ids()), {rid_a, rid_b})
            self.assertEqual(store.get(rid_b)["cpu_limit"].tolist(), original_values)
            forecasts = load_existing_forecast_items(base)
            self.assertEqual({item["resource_id"] for item in forecasts}, {rid_a, rid_b})
            markers = {item["resource_id"]: item["observed_stats"]["marker"] for item in forecasts}
            self.assertEqual(markers, {rid_a: "new-a", rid_b: "old-b"})

    def test_existing_k8s_upsert_trims_aggregate_and_container_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rid = "k8s:a:prod:deployment:api"
            idx = pd.DatetimeIndex([
                "2026-06-30 23:59:59",
                "2026-07-01 00:00:00",
                "2026-07-20 12:00:00",
                "2026-07-31 00:00:00",
            ])
            values = pd.Series([0.1, 0.2, 0.3, 0.4], index=idx)
            resource = _k8s(rid)
            for metric in ("cpu_limit", "cpu_request", "memory_limit", "memory_request"):
                resource[metric] = values.copy()
                resource["container_metrics"]["api"][metric] = values.copy()
            write_raw_resource_dataset(base, [resource], freq="10min")
            latest = pd.Timestamp("2026-07-31 00:10:00")
            latest_ms = int(latest.timestamp() * 1000)
            incoming = {
                "resource_id": rid,
                "resource_type": "k8s_workload",
                "metrics": {
                    metric: {"timestamps": [latest_ms], "values": [0.5]}
                    for metric in ("cpu_limit", "cpu_request", "memory_limit", "memory_request")
                },
                "container_metrics": {
                    "api": {
                        metric: {"timestamps": [latest_ms], "values": [0.5]}
                        for metric in (
                            "cpu_limit", "cpu_request", "memory_limit", "memory_request"
                        )
                    }
                },
            }

            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[]):
                result = run_upsert_with_data(
                    [incoming], out_dir=base, fail_if_busy=True, freq_hint="10min"
                )

            self.assertTrue(result["success"], result.get("error"))
            loaded = RawResourceStore(base).get(rid)
            aggregate = loaded["cpu_limit"]
            container = loaded["container_metrics"]["api"]["cpu_limit"]
            self.assertGreaterEqual(
                aggregate.index[0], aggregate.index[-1] - pd.Timedelta(days=30)
            )
            self.assertGreaterEqual(
                container.index[0], container.index[-1] - pd.Timedelta(days=30)
            )
            self.assertNotIn(idx[0], aggregate.index)
            self.assertNotIn(idx[0], container.index)

    def test_new_k8s_upsert_trims_before_first_raw_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rid = "k8s:a:prod:deployment:new-api"
            idx = pd.DatetimeIndex([
                "2026-06-01 00:00:00",
                "2026-07-01 00:00:00",
                "2026-07-31 00:00:00",
            ])
            timestamps = [int(value.timestamp() * 1000) for value in idx]
            metric_payload = {"timestamps": timestamps, "values": [0.1, 0.2, 0.3]}
            incoming = {
                "resource_id": rid,
                "resource_type": "k8s_workload",
                "spec": {"cluster": "a", "namespace": "prod", "containers": {"api": {}}},
                "metrics": {
                    metric: dict(metric_payload)
                    for metric in ("cpu_limit", "cpu_request", "memory_limit", "memory_request")
                },
                "container_metrics": {
                    "api": {
                        metric: dict(metric_payload)
                        for metric in (
                            "cpu_limit", "cpu_request", "memory_limit", "memory_request"
                        )
                    }
                },
            }

            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[]):
                result = run_upsert_with_data(
                    [incoming], out_dir=base, fail_if_busy=True, freq_hint="10min"
                )

            self.assertTrue(result["success"], result.get("error"))
            loaded = RawResourceStore(base).get(rid)
            self.assertEqual(loaded["cpu_limit"].index.tolist(), idx[1:].tolist())
            self.assertEqual(
                loaded["container_metrics"]["api"]["cpu_limit"].index.tolist(),
                idx[1:].tolist(),
            )

    def test_pull_provider_runs_inside_update_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_raw_resource_dataset(base, [_vm("vm-1")], freq="5min")
            timestamp = int(pd.Timestamp("2026-01-01 00:20:00").timestamp() * 1000)

            def provider(prepared, points):
                self.assertTrue(updater._update_exclusive.locked())
                self.assertEqual(points, 1)
                self.assertEqual([item["resource_id"] for item in prepared], ["vm-1"])
                return [{
                    "resource_id": "vm-1",
                    "metrics": {"cpu": {"timestamps": [timestamp], "values": [0.4]}},
                }]

            with patch.object(updater, "scoped_out_dir", return_value=base):
                with patch("resource_predict.pipeline.generate_predictions_only", return_value=[]):
                    result = updater.run_update(
                        incremental_provider=provider,
                        points_per_update=1,
                        fail_if_busy=True,
                    )

            self.assertTrue(result["success"], result.get("error"))
            self.assertFalse(updater._update_exclusive.locked())


if __name__ == "__main__":
    unittest.main()
