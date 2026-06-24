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
from resource_predict.data.updater import run_update_with_data
from resource_predict.pipeline.constants import RAW_INDEX_FILENAME


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


class RawResourceStoreTest(unittest.TestCase):
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
