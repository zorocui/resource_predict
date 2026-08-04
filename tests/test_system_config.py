import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from resource_predict.api.system_config import register_system_config_routes
from resource_predict.services.runtime_config import (
    RuntimeConfigStore,
    RuntimeConfigValidationError,
    default_runtime_config,
    runtime_config_to_dict,
)
from resource_predict.services.system_config import save_system_config_payload


class SystemConfigTest(unittest.TestCase):
    def test_template_separates_system_and_cluster_configuration_views(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-view="system-config"', template)
        self.assertIn('data-view="cluster-config"', template)
        self.assertIn('id="system-config-view"', template)
        self.assertIn('id="cluster-config-view"', template)
        self.assertIn('id="system-config-save"', template)
        self.assertIn('id="cluster-config-save"', template)

        system_start = template.index('id="system-config-view"')
        cluster_start = template.index('id="cluster-config-view"')
        system_view = template[system_start:cluster_start]
        cluster_view = template[cluster_start:]
        self.assertIn('id="collection-config-list"', system_view)
        self.assertNotIn('id="vm-cluster-list"', system_view)
        self.assertIn('id="vm-cluster-list"', cluster_view)
        self.assertNotIn('id="collection-config-list"', cluster_view)

    def test_frontend_preserves_other_page_when_saving_configuration(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "js" / "index.js").read_text(encoding="utf-8")

        self.assertIn(
            '["system-config", "cluster-config"].includes(app.state.activeView)',
            script,
        )
        self.assertIn("function refreshSystemConfig()", script)
        self.assertIn("async function saveSystemConfig()", script)
        self.assertIn(
            "vm_scaling_clusters: cached.vm_scaling_clusters || {}", script
        )
        self.assertIn(
            "k8s_prometheus_clusters: cached.k8s_prometheus_clusters || []", script
        )
        self.assertIn("runtime: cached.runtime || {}", script)
        self.assertNotIn('/api/forecast-config', script)

    def test_save_writes_all_sections_and_swaps_snapshot(self):
        store = RuntimeConfigStore(default_runtime_config())
        runtime = runtime_config_to_dict(default_runtime_config())
        runtime["collection"]["history_days"] = 14
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("resource_predict.services.k8s_ingest.notify_k8s_scheduler_config_changed") as notify:
                result = save_system_config_payload(
                    {"runtime": runtime, "vm_scaling_clusters": {}, "k8s_prometheus_clusters": []},
                    runtime_path=root / "runtime.json", vm_path=root / "vm.json",
                    k8s_path=root / "k8s.json", store=store,
                )
            self.assertTrue((root / "runtime.json").exists())
            self.assertTrue((root / "vm.json").exists())
            self.assertTrue((root / "k8s.json").exists())
        self.assertEqual(store.snapshot().collection.history_days, 14)
        self.assertEqual(result["runtime"]["collection"]["history_days"], 14)
        notify.assert_called_once_with()

    def test_invalid_runtime_writes_nothing(self):
        store = RuntimeConfigStore(default_runtime_config())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(RuntimeConfigValidationError):
                save_system_config_payload(
                    {"runtime": {"collection": {"step_seconds": 0}}},
                    runtime_path=root / "runtime.json", vm_path=root / "vm.json",
                    k8s_path=root / "k8s.json", store=store,
                )
            self.assertEqual(list(root.iterdir()), [])

    def test_api_returns_field_path(self):
        app = Flask(__name__)
        register_system_config_routes(app)
        error = RuntimeConfigValidationError(
            "runtime.collection.step_seconds", "step_seconds 必须为正整数"
        )
        with patch("resource_predict.api.system_config.save_system_config_payload", side_effect=error):
            response = app.test_client().put("/api/system-config", json={"runtime": {}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["field"], "runtime.collection.step_seconds")


if __name__ == "__main__":
    unittest.main()
