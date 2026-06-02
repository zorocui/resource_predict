import json
import tempfile
import unittest
from pathlib import Path

from resource_predict.services.forecast_config import (
    ForecastConfigValidationError,
    read_forecast_config,
    read_forecast_config_payload,
    write_forecast_config,
)
from resource_predict.settings import settings


class ForecastConfigTest(unittest.TestCase):
    def test_default_keeps_rolling_mean_and_ensemble_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = read_forecast_config(Path(tmp) / "forecast_config.json")

        self.assertEqual(list(settings.forecast.enabled_methods), ["seasonal_naive", "prophet"])
        self.assertEqual(config["enabled_methods"], ["seasonal_naive", "prophet"])
        self.assertFalse(config["enable_ensemble"])
        self.assertTrue(config["reuse_backtest_model_for_future"])
        self.assertTrue(config["prophet_routing_enabled"])
        self.assertEqual(config["prophet_routing_mode"], "auto")

    def test_write_roundtrips_enabled_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecast_config.json"
            written = write_forecast_config(
                {
                    "enabled_methods": ["prophet", "rolling_mean"],
                    "enable_ensemble": True,
                    "reuse_backtest_model_for_future": False,
                    "prophet_routing_enabled": False,
                    "prophet_routing_mode": "always",
                },
                path,
            )
            on_disk = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(written["enabled_methods"], ["prophet", "rolling_mean"])
        self.assertTrue(written["enable_ensemble"])
        self.assertFalse(written["reuse_backtest_model_for_future"])
        self.assertFalse(written["prophet_routing_enabled"])
        self.assertEqual(written["prophet_routing_mode"], "always")
        self.assertEqual(on_disk, written)

    def test_rejects_unknown_or_empty_methods(self):
        with self.assertRaises(ForecastConfigValidationError):
            write_forecast_config({"enabled_methods": ["unknown"]}, Path(tempfile.gettempdir()) / "unused.json")
        with self.assertRaises(ForecastConfigValidationError):
            write_forecast_config({"enabled_methods": []}, Path(tempfile.gettempdir()) / "unused.json")
        with self.assertRaises(ForecastConfigValidationError):
            write_forecast_config(
                {"enabled_methods": ["rolling_mean"], "prophet_routing_mode": "bogus"},
                Path(tempfile.gettempdir()) / "unused.json",
            )

    def test_old_config_files_get_new_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecast_config.json"
            path.write_text(
                json.dumps({"enabled_methods": ["rolling_mean"], "enable_ensemble": False}),
                encoding="utf-8",
            )
            config = read_forecast_config(path)

        self.assertEqual(config["enabled_methods"], ["rolling_mean"])
        self.assertFalse(config["enable_ensemble"])
        self.assertTrue(config["reuse_backtest_model_for_future"])
        self.assertTrue(config["prophet_routing_enabled"])
        self.assertEqual(config["prophet_routing_mode"], "auto")

    def test_payload_includes_supported_methods_for_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = read_forecast_config_payload(Path(tmp) / "forecast_config.json")

        keys = [item["key"] for item in payload["supported_methods"]]
        self.assertIn("prophet", keys)
        self.assertIn("rolling_mean", keys)
        self.assertIn("seasonal_naive", keys)


if __name__ == "__main__":
    unittest.main()
