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
        self.assertEqual(cfg["ssh_key"], "/root/.ssh/id_rsa")
        self.assertEqual(cfg["openstack_rc"], "/root/admin-openstack.sh")
        self.assertEqual(cfg["allowed_flavors"], ["m1.small"])

    def test_scaling_config_accepts_k8s_control_cluster(self):
        payload = normalize_vm_scaling_clusters(
            {
                "cluster-k8s-a": {
                    "cloud_type": "k8s",
                    "control_host": "192.168.1.20",
                    "ssh_user": "root",
                    "kubeconfig": "/root/.kube/config",
                }
            }
        )

        cfg = payload["cluster-k8s-a"]
        self.assertEqual(cfg["cloud_type"], "k8s")
        self.assertEqual(cfg["ssh_port"], 22)
        self.assertEqual(cfg["ssh_key"], "/root/.ssh/id_rsa")
        self.assertEqual(cfg["kubeconfig"], "/root/.kube/config")
        self.assertNotIn("openstack_rc", cfg)

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
        self.assertEqual(saved["cluster-openstack-a"]["ssh_key"], "/root/.ssh/id_rsa")
        self.assertEqual(saved["cluster-openstack-a"]["openstack_rc"], "/root/admin-openstack.sh")

    def test_k8s_ingest_upserts_when_raw_exists(self):
        items = [
            {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {"cpu": {"timestamps": [1], "values": [0.2]}, "memory": {"timestamps": [1], "values": [0.3]}},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            raw_index_path = Path(tmp) / "k8s" / "raw_index.json"
            raw_index_path.parent.mkdir(parents=True)
            raw_index_path.write_text(
                json.dumps({
                    "schema_version": 2,
                    "resources": {
                        "k8s:cluster-a:ns:deployment:api": {
                            "file": "raw/00/unused.json",
                            "resource_type": "k8s_workload",
                        }
                    },
                }),
                encoding="utf-8",
            )
            fake_settings = SimpleNamespace(
                app=SimpleNamespace(out_dir=tmp),
                k8s_prometheus=SimpleNamespace(
                    scheduled_update_interval_minutes=360,
                    incremental_overlap_minutes=60,
                    step_seconds=600,
                ),
            )
            with patch.object(k8s_ingest, "settings", fake_settings):
                with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
                    "items": items,
                    "cluster_results": [{"cluster": "cluster-a", "status": "success", "resources_fetched": 1}],
                }) as fetch:
                    with patch.object(k8s_ingest, "run_upsert_with_data", return_value={"success": True}) as upsert:
                        with patch.object(k8s_ingest, "mark_external_update_finished") as mark_finished:
                            result = k8s_ingest.run_k8s_prometheus_upsert(clusters=["cluster-a"], fail_if_busy=True)

        self.assertTrue(result["success"])
        fetch.assert_called_once_with(["cluster-a"], history_hours=7.0)
        mark_finished.assert_called_once_with(result)
        upsert.assert_called_once_with(
            items,
            fail_if_busy=True,
            out_dir=Path(tmp) / "k8s",
            freq_hint="600s",
            task_source="页面手动拉取",
            _exclusive_already_acquired=True,
        )

    def test_k8s_ingest_uses_full_window_without_raw_baseline(self):
        items = [
            {
                "resource_id": "k8s:cluster-a:ns:deployment:api",
                "resource_type": "k8s_workload",
                "metrics": {"cpu": {"timestamps": [1], "values": [0.2]}, "memory": {"timestamps": [1], "values": [0.3]}},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_settings = SimpleNamespace(
                app=SimpleNamespace(out_dir=tmp),
                k8s_prometheus=SimpleNamespace(
                    scheduled_update_interval_minutes=360,
                    incremental_overlap_minutes=60,
                ),
            )
            with patch.object(k8s_ingest, "settings", fake_settings):
                with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
                    "items": items,
                    "cluster_results": [{"cluster": "cluster-a", "status": "success", "resources_fetched": 1}],
                }) as fetch:
                    with patch.object(k8s_ingest, "run_upsert_with_data", return_value={"success": True}):
                        result = k8s_ingest.run_k8s_prometheus_upsert(clusters=["cluster-a"], fail_if_busy=True)

        self.assertTrue(result["success"])
        fetch.assert_called_once_with(["cluster-a"], history_hours=None)

    def test_k8s_ingest_preserves_partial_success(self):
        items = [{"resource_id": "k8s:cluster-a:ns:deployment:api"}]
        cluster_results = [
            {"cluster": "cluster-a", "status": "success", "resources_fetched": 1},
            {"cluster": "cluster-b", "status": "failed", "resources_fetched": 0, "error": "timeout"},
        ]
        fake_settings = SimpleNamespace(
            app=SimpleNamespace(out_dir="outputs"),
            k8s_prometheus=SimpleNamespace(scheduled_update_interval_minutes=360, incremental_overlap_minutes=60),
        )
        with patch.object(k8s_ingest, "settings", fake_settings):
            with patch.object(k8s_ingest, "_has_existing_k8s_raw_data", return_value=True):
                with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
                    "items": items, "cluster_results": cluster_results,
                }):
                    with patch.object(k8s_ingest, "run_upsert_with_data", return_value={"success": True}):
                        with patch.object(k8s_ingest, "mark_external_update_finished") as finished:
                            result = k8s_ingest.run_k8s_prometheus_upsert()

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(result["cluster_results"], cluster_results)
        finished.assert_called_once_with(result)

    def test_k8s_ingest_all_cluster_failures_skip_upsert_and_are_recorded(self):
        cluster_results = [
            {"cluster": "cluster-a", "status": "failed", "resources_fetched": 0, "error": "timeout"}
        ]
        with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
            "items": [], "cluster_results": cluster_results,
        }):
            with patch.object(k8s_ingest, "run_upsert_with_data") as upsert:
                with patch.object(k8s_ingest, "mark_external_update_failed") as failed:
                    with self.assertRaisesRegex(RuntimeError, "所有 K8S Prometheus 集群拉取失败"):
                        k8s_ingest.run_k8s_prometheus_upsert()

        upsert.assert_not_called()
        self.assertEqual(failed.call_args.kwargs["cluster_results"], cluster_results)

    def test_k8s_ingest_downstream_failure_overrides_cluster_success(self):
        cluster_results = [
            {"cluster": "cluster-a", "status": "success", "resources_fetched": 1}
        ]
        with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
            "items": [{"resource_id": "k8s:cluster-a:ns:deployment:api"}],
            "cluster_results": cluster_results,
        }):
            with patch.object(k8s_ingest, "run_upsert_with_data", return_value={
                "success": False, "error": "raw write failed",
            }):
                with patch.object(k8s_ingest, "mark_external_update_failed") as failed:
                    result = k8s_ingest.run_k8s_prometheus_upsert()

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(failed.call_args.kwargs["cluster_results"], cluster_results)


def test_new_cluster_gets_full_history_alongside_existing_clusters(tmp_path):
    from resource_predict.providers import k8s_prometheus as provider

    out_dir = tmp_path / "k8s"
    out_dir.mkdir()
    (out_dir / "raw_index.json").write_text(json.dumps({
        "schema_version": 2,
        "resources": {f"k8s:{name}:ns:deployment:api": {
            "file": "raw/00/unused.json", "resource_type": "k8s_workload",
        } for name in ("old-a", "old-b")},
    }), encoding="utf-8")
    fake_settings = SimpleNamespace(app=SimpleNamespace(out_dir=str(tmp_path)),
        k8s_prometheus=SimpleNamespace(scheduled_update_interval_minutes=360,
                                      incremental_overlap_minutes=0))
    targets = [SimpleNamespace(cluster=name, prometheus_url=f"http://{name}")
               for name in ("old-a", "old-b", "new")]
    with patch.object(k8s_ingest, "settings", fake_settings), \
         patch.object(provider, "_resolve_targets", return_value=targets), \
         patch.object(provider, "_fetch_target_with_retry", return_value=[{"resource_id": "test"}]) as fetch:
        for selected, full_refresh, expected in [
            (None, False, [6.0, 6.0, None]),
            (["old-a", "old-b", "new"], False, [6.0, 6.0, None]),
            (["new"], False, [None]),
            (["old-a"], False, [6.0]),
            (None, True, [None, None, None]),
        ]:
            fetch.reset_mock()
            hours = k8s_ingest._history_hours_for_fetch(
                out_dir=out_dir, clusters=selected, full_refresh=full_refresh)
            result = k8s_ingest.fetch_k8s_prometheus_result(selected, history_hours=hours)
            assert [call.kwargs["history_hours"] for call in fetch.call_args_list] == expected
            assert all(row["status"] == "success" for row in result["cluster_results"])


if __name__ == "__main__":
    unittest.main()
