from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from resource_predict.services.scaling import snapshot
from resource_predict.services.scaling.snapshot import _merge_spec


def test_merge_spec_patches_container_targets_without_dropping_untouched_containers():
    current = {
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
        "replicas_observed": 2,
    }
    effective = {
        "containers": {
            "app": {
                "cpu_request_cores": 0.8,
                "cpu_limit_cores": None,
            }
        },
        "replicas": 3,
    }

    merged = _merge_spec(current, effective)

    assert merged["containers"]["app"]["cpu_request_cores"] == 0.8
    assert merged["containers"]["app"]["cpu_limit_cores"] == 1.0
    assert merged["containers"]["sidecar"]["cpu_request_cores"] == 0.1
    assert merged["containers"]["sidecar"]["memory_limit_gb"] == 0.2
    assert merged["replicas"] == 3


def test_scaling_success_updates_sharded_raw_and_prediction_snapshots():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out_dir = root / "vm"
        index = pd.date_range("2026-01-01", periods=8, freq="h")
        values = pd.Series([0.2] * 8, index=index)
        raw = {
            "resource_id": "vm-1",
            "resource_type": "openstack_vm",
            "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
            "cpu": values,
            "memory": values,
            "disk": values,
        }
        write_raw_resource_dataset(out_dir, [raw], freq="h")
        summary_item = {
            "resource_id": "vm-1",
            "resource_type": "openstack_vm",
            "spec": dict(raw["spec"]),
            "scaling_advice": {"action": "hold"},
            "detail_ref": {"file": "part-00000.json", "offset": 0},
        }
        detail_item = {**summary_item, "charts_forecast": {}}
        (out_dir / "details").mkdir(parents=True)
        (out_dir / "summary_index.json").write_text(
            json.dumps({"resources": [summary_item]}), encoding="utf-8"
        )
        (out_dir / "details" / "part-00000.json").write_text(
            json.dumps({"resources": [detail_item]}), encoding="utf-8"
        )
        (out_dir / "manifest.json").write_text(
            json.dumps({"resources": [detail_item]}), encoding="utf-8"
        )
        plan = SimpleNamespace(
            resource_id="vm-1",
            resource_type="openstack_vm",
            target_spec={"cpu_cores": 4, "memory_gb": 8, "disk_gb": 80},
            details={},
        )
        fake_settings = SimpleNamespace(
            app=SimpleNamespace(out_dir=str(root)),
            generation=SimpleNamespace(raw_resource_cache_items=10, freq="h"),
        )

        with patch.object(snapshot, "settings", fake_settings):
            result = snapshot.apply_scaling_success_snapshot(plan)

        loaded_raw = RawResourceStore(out_dir).get("vm-1")
        loaded_summary = json.loads((out_dir / "summary_index.json").read_text(encoding="utf-8"))
        loaded_detail = json.loads((out_dir / "details" / "part-00000.json").read_text(encoding="utf-8"))
        loaded_manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

    assert result["raw_updated"] is True
    assert result["summary_updated"] is True
    assert result["detail_updated"] is True
    assert result["manifest_updated"] is True
    for item in (
        loaded_raw,
        loaded_summary["resources"][0],
        loaded_detail["resources"][0],
        loaded_manifest["resources"][0],
    ):
        assert item["spec"]["cpu_cores"] == 4
        assert item["spec"]["memory_gb"] == 8
        assert item["spec"]["disk_gb"] == 80
