from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from resource_predict.services.output_health import check_outputs


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def metric_block() -> dict:
    return {"timestamps": [1_700_000_000_000, 1_700_003_600_000], "values": [0.3, 0.4]}


def valid_artifacts() -> tuple[dict, dict, dict]:
    vm_summary = {
        "resource_id": "vm-001",
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
        "scaling_advice": {"action": "hold"},
        "detail_ref": {"file": "part-00000.json", "offset": 0},
    }
    k8s_summary = {
        "resource_id": "k8s:cluster-a:ns:deployment:api",
        "resource_type": "k8s_workload",
        "spec": {
            "cluster": "cluster-a",
            "namespace": "ns",
            "workload_kind": "Deployment",
            "workload_name": "api",
            "pods_observed": ["api-rs-a", "api-rs-b"],
            "containers_observed": ["app"],
            "replicas_observed": 2,
        },
        "scaling_advice": {
            "resource_type": "k8s_workload",
            "action": "hold",
            "target_k8s_policy": {"recommendations": {}},
            "analysis_only": True,
        },
        "detail_ref": {"file": "part-00000.json", "offset": 1},
        "charts_forecast": {"cpu": {}, "memory": {}},
    }
    summary = {
        "meta": {"details_files": ["part-00000.json"], "details_dir": "details"},
        "resources": [vm_summary, k8s_summary],
    }
    raw = {
        "meta": {"schema_version": 1},
        "resources": [
            {
                "resource_id": "vm-001",
                "resource_type": "openstack_vm",
                "metrics": {"cpu": metric_block(), "memory": metric_block(), "disk": metric_block()},
            },
            {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {"cpu": metric_block(), "memory": metric_block()},
            },
        ],
    }
    details = {"resources": [dict(vm_summary), dict(k8s_summary)]}
    return summary, raw, details


class OutputHealthTest(unittest.TestCase):
    def test_check_outputs_accepts_vm_and_k8s_workload_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            summary, raw, details = valid_artifacts()
            write_json(base / "summary_index.json", summary)
            write_json(base / "raw_data.json", raw)
            write_json(base / "details" / "part-00000.json", details)

            report = check_outputs(base)

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["summary_counts"]["openstack_vm"], 1)
        self.assertEqual(report["summary_counts"]["k8s_workload"], 1)
        self.assertEqual(
            report["sample_workloads"],
            [
                {
                    "resource_id": "k8s:cluster-a:ns:deployment:api",
                    "namespace": "ns",
                    "workload_kind": "Deployment",
                    "workload_name": "api",
                }
            ],
        )

    def test_check_outputs_rejects_legacy_pod(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            summary, raw, details = valid_artifacts()
            summary["resources"][1]["resource_type"] = "k8s_pod"
            raw["resources"][1]["resource_type"] = "k8s_pod"
            details["resources"][1]["resource_type"] = "k8s_pod"
            write_json(base / "summary_index.json", summary)
            write_json(base / "raw_data.json", raw)
            write_json(base / "details" / "part-00000.json", details)

            report = check_outputs(base)

        self.assertFalse(report["ok"])
        self.assertTrue(any("旧 Pod 粒度资源" in err for err in report["errors"]))
        self.assertTrue(any("缺少 resource_type=k8s_workload" in err for err in report["errors"]))

    def test_check_outputs_rejects_detail_missing_k8s_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            summary, raw, details = valid_artifacts()
            details["resources"][1]["scaling_advice"].pop("target_k8s_policy")
            write_json(base / "summary_index.json", summary)
            write_json(base / "raw_data.json", raw)
            write_json(base / "details" / "part-00000.json", details)

            report = check_outputs(base)

        self.assertFalse(report["ok"])
        self.assertTrue(any("details.target_k8s_policy 缺失" in err for err in report["errors"]))


if __name__ == "__main__":
    unittest.main()
