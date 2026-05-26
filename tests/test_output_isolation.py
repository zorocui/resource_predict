from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resource_predict.data.io import read_raw_dataset, write_raw_dataset
from resource_predict.data.updater import run_upsert_with_data
from resource_predict.services.output_health import check_outputs
from resource_predict.services.store.forecast_store import ForecastStore
from resource_predict.settings import AppConfig


def metric_block() -> dict:
    return {"timestamps": [1_700_000_000_000, 1_700_003_600_000], "values": [0.3, 0.4]}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_artifacts(base: Path, resource_id: str, resource_type: str, metrics: tuple[str, ...]) -> None:
    summary_item = {
        "resource_id": resource_id,
        "resource_type": resource_type,
        "spec": {},
        "scaling_advice": {"action": "hold"},
        "detail_ref": {"file": "part-00000.json", "offset": 0},
    }
    raw_item = {
        "resource_id": resource_id,
        "resource_type": resource_type,
        "metrics": {metric: metric_block() for metric in metrics},
    }
    detail_item = {
        **summary_item,
        "charts_forecast": {metric: {} for metric in metrics},
    }
    if resource_type == "k8s_workload":
        spec = {
            "cluster": "cluster-a",
            "namespace": "ns",
            "workload_kind": "Deployment",
            "workload_name": "api",
            "pods_observed": ["api-a"],
            "containers_observed": ["app"],
            "replicas_observed": 1,
        }
        summary_item["spec"] = spec
        detail_item["spec"] = spec
        advice = {
            "resource_type": "k8s_workload",
            "action": "hold",
            "analysis_only": True,
            "target_k8s_policy": {},
        }
        summary_item["scaling_advice"] = advice
        detail_item["scaling_advice"] = advice
    write_json(base / "summary_index.json", {"meta": {"details_files": ["part-00000.json"]}, "resources": [summary_item]})
    write_json(base / "raw_data.json", {"meta": {"schema_version": 1}, "resources": [raw_item]})
    write_json(base / "details" / "part-00000.json", {"resources": [detail_item]})


class OutputIsolationTest(unittest.TestCase):
    def test_k8s_upsert_initializes_scoped_raw_without_touching_vm_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vm_resource = {
                "resource_id": "vm-001",
                "resource_type": "openstack_vm",
                "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
                "cpu": __import__("pandas").Series([0.2, 0.3], index=__import__("pandas").date_range("2026-01-01", periods=2, freq="h")),
                "memory": __import__("pandas").Series([0.4, 0.5], index=__import__("pandas").date_range("2026-01-01", periods=2, freq="h")),
                "disk": __import__("pandas").Series([0.6, 0.7], index=__import__("pandas").date_range("2026-01-01", periods=2, freq="h")),
            }
            vm_raw = root / "vm" / "raw_data.json"
            write_raw_dataset(vm_raw, [vm_resource], freq="h")
            before = vm_raw.read_text(encoding="utf-8")

            k8s_item = {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {"cpu": metric_block(), "memory": metric_block()},
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
            with patch("resource_predict.pipeline.generate_predictions_only", return_value=[{"resource_id": k8s_item["resource_id"]}]) as mock_generate:
                result = run_upsert_with_data([k8s_item], out_dir=root / "k8s", fail_if_busy=True)

            self.assertTrue(result["success"], result)
            mock_generate.assert_called_once()
            self.assertEqual(str(root / "k8s"), mock_generate.call_args.kwargs.get("out_dir"))
            self.assertEqual(before, vm_raw.read_text(encoding="utf-8"))
            prepared, _meta = read_raw_dataset(root / "k8s" / "raw_data.json")
            self.assertEqual([x["resource_id"] for x in prepared], [k8s_item["resource_id"]])

    def test_store_and_health_merge_scoped_vm_and_k8s_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_artifacts(root / "vm", "vm-001", "openstack_vm", ("cpu", "memory", "disk"))
            write_artifacts(root / "k8s", "k8s:cluster-a:ns:deployment:api", "k8s_workload", ("cpu", "memory"))

            store = ForecastStore(app_cfg=AppConfig(out_dir=str(root)))
            summary = store.get_summary()
            health = check_outputs(root)

        ids = {item["resource_id"] for item in summary["resources"]}
        self.assertEqual(ids, {"vm-001", "k8s:cluster-a:ns:deployment:api"})
        self.assertTrue(health["ok"], health)
        self.assertEqual(health["summary_counts"], {"k8s_workload": 1, "openstack_vm": 1})


if __name__ == "__main__":
    unittest.main()
