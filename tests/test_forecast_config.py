import json
import tempfile
import unittest
from pathlib import Path

from resource_predict.services.forecast_config import (
    ForecastConfigValidationError,
    normalize_forecast_config_payload,
    read_forecast_config,
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

    def test_normalizes_supported_methods_and_switches(self):
        normalized = normalize_forecast_config_payload(
            {
                "enabled_methods": ["prophet", "rolling_mean"],
                "enable_ensemble": True,
                "reuse_backtest_model_for_future": False,
                "prophet_routing_enabled": False,
                "prophet_routing_mode": "always",
            }
        )

        self.assertEqual(normalized["enabled_methods"], ["prophet", "rolling_mean"])
        self.assertTrue(normalized["enable_ensemble"])
        self.assertFalse(normalized["reuse_backtest_model_for_future"])
        self.assertFalse(normalized["prophet_routing_enabled"])
        self.assertEqual(normalized["prophet_routing_mode"], "always")

    def test_rejects_unknown_or_empty_methods(self):
        with self.assertRaises(ForecastConfigValidationError):
            normalize_forecast_config_payload({"enabled_methods": ["unknown"]})
        with self.assertRaises(ForecastConfigValidationError):
            normalize_forecast_config_payload({"enabled_methods": []})
        with self.assertRaises(ForecastConfigValidationError):
            normalize_forecast_config_payload(
                {"enabled_methods": ["rolling_mean"], "prophet_routing_mode": "bogus"}
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


if __name__ == "__main__":
    unittest.main()
