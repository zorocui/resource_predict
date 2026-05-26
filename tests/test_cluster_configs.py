from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from resource_predict.services.cluster_configs import (
    ClusterConfigValidationError,
    normalize_k8s_prometheus_clusters,
    normalize_vm_scaling_clusters,
    read_k8s_prometheus_clusters,
    write_k8s_prometheus_clusters,
    write_vm_scaling_clusters,
)
from resource_predict.services import k8s_ingest


class ClusterConfigsTest(unittest.TestCase):
    def test_vm_scaling_config_normalizes_required_fields(self):
        payload = normalize_vm_scaling_clusters(
            {
                "cluster-openstack-a": {
                    "control_host": "192.168.1.10",
                    "ssh_user": "root",
                    "ssh_port": "2222",
                    "allowed_flavors": ["m1.small"],
                }
            }
        )

        cfg = payload["cluster-openstack-a"]
        self.assertEqual(cfg["cloud_type"], "openstack")
        self.assertEqual(cfg["ssh_port"], 2222)
        self.assertEqual(cfg["allowed_flavors"], ["m1.small"])

    def test_vm_scaling_config_requires_control_host(self):
        with self.assertRaises(ClusterConfigValidationError):
            normalize_vm_scaling_clusters({"cluster-a": {"ssh_user": "root"}})

    def test_k8s_prometheus_config_accepts_env_object_shape(self):
        payload = normalize_k8s_prometheus_clusters(
            {"cluster-k8s-a": {"prometheus_url": "http://prometheus:9090", "namespace_regex": "prod"}}
        )

        self.assertEqual(payload[0]["cluster"], "cluster-k8s-a")
        self.assertEqual(payload[0]["prometheus_url"], "http://prometheus:9090")
        self.assertEqual(payload[0]["namespace_regex"], "prod")

    def test_k8s_prometheus_config_roundtrips_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k8s.json"
            write_k8s_prometheus_clusters(
                [{"cluster": "cluster-k8s-a", "prometheus_url": "http://prometheus:9090"}],
                path,
            )

            data = json.loads(path.read_text(encoding="utf-8"))
            loaded = read_k8s_prometheus_clusters(path)

        self.assertEqual(data[0]["cluster"], "cluster-k8s-a")
        self.assertEqual(loaded[0]["prometheus_url"], "http://prometheus:9090")

    def test_vm_scaling_config_roundtrips_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clusters.json"
            saved = write_vm_scaling_clusters(
                {
                    "cluster-openstack-a": {
                        "control_host": "192.168.1.10",
                        "ssh_user": "root",
                    }
                },
                path,
            )

        self.assertEqual(saved["cluster-openstack-a"]["ssh_port"], 22)

    def test_k8s_ingest_upserts_when_raw_exists(self):
        items = [
            {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {"cpu": {"timestamps": [1], "values": [0.2]}, "memory": {"timestamps": [1], "values": [0.3]}},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "raw_data.json"
            raw_path.write_text("{}", encoding="utf-8")
            with patch.object(k8s_ingest, "settings", SimpleNamespace(app=SimpleNamespace(out_dir=tmp))):
                with patch.object(k8s_ingest, "fetch_k8s_prometheus_items", return_value=items):
                    with patch.object(k8s_ingest, "run_upsert_with_data", return_value={"success": True}) as upsert:
                        result = k8s_ingest.run_k8s_prometheus_upsert(clusters=["cluster-a"], fail_if_busy=True)

        self.assertTrue(result["success"])
        upsert.assert_called_once_with(
            items,
            fail_if_busy=True,
            out_dir=Path(tmp) / "k8s",
        )


if __name__ == "__main__":
    unittest.main()
