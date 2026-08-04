import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from resource_predict.services.runtime_config import (
    RuntimeConfigStore,
    RuntimeConfigValidationError,
    default_runtime_config,
    load_runtime_config,
    normalize_runtime_config,
    runtime_config_to_dict,
    write_runtime_config,
)


class RuntimeConfigTest(unittest.TestCase):
    def test_defaults_expose_only_runtime_whitelist(self):
        payload = runtime_config_to_dict(default_runtime_config())
        self.assertEqual(set(payload), {"collection", "prediction", "decision"})
        self.assertEqual(payload["collection"]["rate_window"], "5m")
        self.assertNotIn("fail_fast", payload["collection"])
        self.assertEqual(sum(len(v) for v in payload.values()), 22)

    def test_unknown_field_reports_stable_path(self):
        with self.assertRaises(RuntimeConfigValidationError) as caught:
            normalize_runtime_config({"collection": {"unknown": 1}})
        self.assertEqual(caught.exception.field, "runtime.collection.unknown")

    def test_invalid_ratio_does_not_replace_snapshot(self):
        store = RuntimeConfigStore(default_runtime_config())
        before = store.snapshot()
        with self.assertRaises(RuntimeConfigValidationError):
            store.replace_payload({"decision": {"scale_out_threshold": 1.1}})
        self.assertIs(store.snapshot(), before)

    def test_legacy_forecast_is_used_only_without_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = Path(tmp) / "runtime.json"
            legacy_path = Path(tmp) / "forecast.json"
            legacy_path.write_text(json.dumps({
                "enabled_methods": ["rolling_mean"], "enable_ensemble": True,
            }), encoding="utf-8")
            migrated, _ = load_runtime_config(runtime_path, legacy_path)
            self.assertEqual(migrated.prediction.enabled_methods, ("rolling_mean",))
            self.assertTrue(migrated.prediction.enable_ensemble)
            write_runtime_config(default_runtime_config(), runtime_path)
            loaded, _ = load_runtime_config(runtime_path, legacy_path)
            self.assertEqual(loaded, default_runtime_config())

    def test_utf8_roundtrip(self):
        payload = runtime_config_to_dict(default_runtime_config())
        payload["decision"]["conservative_namespaces"] = ["生产", "核心"]
        config = normalize_runtime_config(payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            write_runtime_config(config, path)
            loaded, warnings = load_runtime_config(path)
        self.assertEqual(warnings, [])
        self.assertEqual(loaded.decision.conservative_namespaces, ("生产", "核心"))

    def test_broken_file_uses_defaults_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text("{broken", encoding="utf-8")
            loaded, warnings = load_runtime_config(path)
        self.assertEqual(loaded, default_runtime_config())
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
