import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from resource_predict.api.system_config import register_system_config_routes
from resource_predict.internal_settings import settings
from resource_predict.services.runtime_config import (
    RuntimeConfigStore,
    RuntimeConfigValidationError,
    default_runtime_config,
    runtime_config_store,
    runtime_config_to_dict,
    write_runtime_config,
)
from resource_predict.services.system_config import save_system_config_payload


class SystemConfigTest(unittest.TestCase):
    def test_collection_reliability_defaults_and_roundtrip(self):
        runtime = runtime_config_to_dict(default_runtime_config())
        collection = runtime["collection"]
        self.assertEqual(collection["range_query_chunk_hours"], 24)
        self.assertEqual(collection["request_max_attempts"], 3)
        self.assertEqual(collection["retry_backoff_seconds"], 1.0)
        self.assertEqual(collection["max_interpolation_gap_steps"], 3)
        self.assertEqual(collection["retention_days"], 30)

        store = RuntimeConfigStore(default_runtime_config())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            normalized = store.replace_payload(runtime)
            write_runtime_config(normalized, path)
            persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["collection"], collection)

    def test_collection_reliability_values_must_be_positive(self):
        for field, value in (
            ("range_query_chunk_hours", 0),
            ("request_max_attempts", 0),
            ("retry_backoff_seconds", 0),
            ("max_interpolation_gap_steps", 0),
        ):
            payload = runtime_config_to_dict(default_runtime_config())
            payload["collection"][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeConfigValidationError, field):
                    RuntimeConfigStore(default_runtime_config()).replace_payload(payload)

    def test_retry_backoff_must_be_numeric_finite_and_positive(self):
        for value in ("1", -1, float("inf"), float("nan")):
            payload = runtime_config_to_dict(default_runtime_config())
            payload["collection"]["retry_backoff_seconds"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    RuntimeConfigValidationError, "retry_backoff_seconds"
                ):
                    RuntimeConfigStore(default_runtime_config()).replace_payload(payload)

    def test_collection_reliability_values_are_exposed_by_settings(self):
        original = runtime_config_store.snapshot()
        payload = runtime_config_to_dict(original)
        payload["collection"].update(
            range_query_chunk_hours=12,
            request_max_attempts=5,
            retry_backoff_seconds=0.25,
            max_interpolation_gap_steps=2,
        )
        try:
            runtime_config_store.replace_payload(payload)
            config = settings.k8s_prometheus
            self.assertEqual(config.range_query_chunk_hours, 12)
            self.assertEqual(config.request_max_attempts, 5)
            self.assertEqual(config.retry_backoff_seconds, 0.25)
            self.assertEqual(config.max_interpolation_gap_steps, 2)
        finally:
            runtime_config_store.replace(original)

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
        self.assertIn('"retention_days"', script)
        self.assertIn('retention_days: rowValue(collection, "retention_days")', script)
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
