from __future__ import annotations

import dataclasses
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from resource_predict.core.forecasting import (
    ForecastResult,
    clip_usage_range,
    ensure_regular_freq,
    forecast_arima,
    forecast_rolling_mean,
    forecast_sarima,
    forecast_seasonal_naive,
    infer_pandas_freq,
    infer_steps_per_day,
    usage_forecast_upper_bound,
)


def _make_series(n: int = 200, freq: str = "h", seed: int = 42) -> pd.Series:
    rng = np.random.RandomState(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq=freq)
    return pd.Series(rng.rand(n) * 0.5 + 0.2, index=idx, name="cpu")


class ForecastResultTest(unittest.TestCase):
    def test_creation_stores_fields(self):
        yhat = pd.Series([1.0, 2.0, 3.0])
        result = ForecastResult(yhat=yhat, seconds=0.5)

        self.assertIs(result.yhat, yhat)
        self.assertEqual(result.seconds, 0.5)

    def test_frozen_prevents_mutation(self):
        result = ForecastResult(yhat=pd.Series([1.0]), seconds=1.0)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.seconds = 2.0  # type: ignore[misc]


class UsageForecastUpperBoundTest(unittest.TestCase):
    def test_fixed_mode_returns_configured_value(self):
        y = _make_series()
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            upper = usage_forecast_upper_bound(y)

        self.assertEqual(upper, 1.0)

    def test_auto_mode_scales_with_train_max(self):
        y = pd.Series([0.5, 0.8, 0.9, 0.7])
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "auto_train_max",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.1,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            upper = usage_forecast_upper_bound(y)

        # max=0.9, slack=0.1 => 0.9*1.1=0.99 < fixed=1.0 => returns 1.0
        self.assertEqual(upper, 1.0)

    def test_auto_mode_exceeds_fixed_when_high_values(self):
        y = pd.Series([0.5, 1.2, 0.9])
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "auto_train_max",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.05,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            upper = usage_forecast_upper_bound(y)

        # max=1.2, slack=0.05 => 1.2*1.05=1.26 > fixed=1.0
        self.assertAlmostEqual(upper, 1.26)

    def test_auto_mode_empty_series_falls_back_to_fixed(self):
        y = pd.Series([], dtype=float)
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "auto_train_max",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            upper = usage_forecast_upper_bound(y)

        self.assertEqual(upper, 1.0)

    def test_unknown_mode_raises(self):
        y = _make_series(n=10)
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "bogus",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            with self.assertRaises(ValueError):
                usage_forecast_upper_bound(y)


class ClipUsageRangeTest(unittest.TestCase):
    def test_basic_clipping(self):
        y = pd.Series([-0.1, 0.5, 1.5, 2.0])
        clipped = clip_usage_range(y, upper=1.0)

        self.assertEqual(clipped.iloc[0], 0.0)  # default lower=0
        self.assertEqual(clipped.iloc[1], 0.5)
        self.assertEqual(clipped.iloc[2], 1.0)
        self.assertEqual(clipped.iloc[3], 1.0)

    def test_negative_lower_bound(self):
        y = pd.Series([-5.0, 0.0, 5.0])
        clipped = clip_usage_range(y, upper=3.0, lower=-2.0)

        self.assertEqual(clipped.iloc[0], -2.0)
        self.assertEqual(clipped.iloc[1], 0.0)
        self.assertEqual(clipped.iloc[2], 3.0)


class InferStepsPerDayTest(unittest.TestCase):
    def test_hourly_index(self):
        idx = pd.date_range("2024-01-01", periods=48, freq="h")
        self.assertEqual(infer_steps_per_day(idx), 24)

    def test_five_minute_index(self):
        idx = pd.date_range("2024-01-01", periods=288, freq="5min")
        self.assertEqual(infer_steps_per_day(idx), 288)

    def test_fifteen_minute_index(self):
        idx = pd.date_range("2024-01-01", periods=96, freq="15min")
        self.assertEqual(infer_steps_per_day(idx), 96)

    def test_short_index_returns_default(self):
        idx = pd.date_range("2024-01-01", periods=2, freq="h")
        self.assertEqual(infer_steps_per_day(idx), 24)


class InferPandasFreqTest(unittest.TestCase):
    def test_hourly(self):
        idx = pd.date_range("2024-01-01", periods=48, freq="h")
        self.assertEqual(infer_pandas_freq(idx), "h")

    def test_daily(self):
        idx = pd.date_range("2024-01-01", periods=30, freq="D")
        self.assertEqual(infer_pandas_freq(idx), "D")

    def test_five_minute(self):
        idx = pd.date_range("2024-01-01", periods=100, freq="5min")
        self.assertIn("5", infer_pandas_freq(idx))


class EnsureRegularFreqTest(unittest.TestCase):
    def test_regular_series_unchanged(self):
        y = _make_series(n=50, freq="h")
        result = ensure_regular_freq(y)

        self.assertEqual(len(result), len(y))
        self.assertFalse(result.isna().any())

    def test_non_datetime_index_raises(self):
        y = pd.Series([1, 2, 3], index=[0, 1, 2])

        with self.assertRaises(TypeError):
            ensure_regular_freq(y)


class ForecastArimaTest(unittest.TestCase):
    def test_returns_forecast_result_with_correct_length(self):
        y = _make_series(n=200, freq="h")
        steps = 24
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_arima(y, steps)

        self.assertIsInstance(result, ForecastResult)
        self.assertEqual(len(result.yhat), steps)
        self.assertGreaterEqual(result.seconds, 0.0)

    def test_constant_series(self):
        idx = pd.date_range("2024-01-01", periods=100, freq="h")
        y = pd.Series([0.5] * 100, index=idx, name="cpu")
        steps = 12
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_arima(y, steps)

        self.assertEqual(len(result.yhat), steps)
        self.assertGreaterEqual(result.seconds, 0.0)


class ForecastSarimaTest(unittest.TestCase):
    def test_returns_forecast_result_with_correct_length(self):
        y = _make_series(n=200, freq="h")
        steps = 24
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_sarima(y, steps)

        self.assertIsInstance(result, ForecastResult)
        self.assertEqual(len(result.yhat), steps)
        self.assertGreaterEqual(result.seconds, 0.0)


class ForecastSeasonalNaiveTest(unittest.TestCase):
    def test_returns_correct_length(self):
        y = _make_series(n=200, freq="h")
        steps = 48
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_seasonal_naive(y, steps)

        self.assertIsInstance(result, ForecastResult)
        self.assertEqual(len(result.yhat), steps)
        self.assertGreaterEqual(result.seconds, 0.0)

    def test_replays_last_season(self):
        idx = pd.date_range("2024-01-01", periods=48, freq="h")
        pattern = list(range(24))
        y = pd.Series(pattern + pattern, index=idx, name="cpu", dtype=float)
        steps = 24
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 100.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_seasonal_naive(y, steps, season_length=24)

        np.testing.assert_array_equal(result.yhat.values, pattern)


class ForecastRollingMeanTest(unittest.TestCase):
    def test_returns_forecast_result_with_correct_length(self):
        y = _make_series(n=200, freq="h")
        steps = 24
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_rolling_mean(y, steps)

        self.assertIsInstance(result, ForecastResult)
        self.assertEqual(len(result.yhat), steps)
        self.assertGreaterEqual(result.seconds, 0.0)

    def test_constant_values(self):
        idx = pd.date_range("2024-01-01", periods=100, freq="h")
        y = pd.Series([0.3] * 100, index=idx, name="cpu")
        steps = 10
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "fixed",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.03,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_rolling_mean(y, steps)

        # Rolling mean of constant series should be that constant
        np.testing.assert_array_almost_equal(result.yhat.values, [0.3] * steps)


class TrendingSeriesTest(unittest.TestCase):
    def test_arima_handles_trending_series(self):
        idx = pd.date_range("2024-01-01", periods=200, freq="h")
        y = pd.Series(np.linspace(0.1, 0.9, 200), index=idx, name="cpu")
        steps = 24
        cfg = type(
            "Cfg",
            (),
            {
                "usage_clip_upper_mode": "auto_train_max",
                "usage_clip_upper_fixed": 1.0,
                "usage_clip_upper_slack": 0.1,
            },
        )()
        settings_stub = type("Settings", (), {"forecast": cfg})()

        with patch("resource_predict.core.forecasting.settings", settings_stub):
            result = forecast_arima(y, steps)

        self.assertEqual(len(result.yhat), steps)
        self.assertGreaterEqual(result.seconds, 0.0)
        # Values should be clipped to upper bound
        self.assertTrue(all(v <= 1.1 * 0.9 * 1.1 for v in result.yhat))


if __name__ == "__main__":
    unittest.main()
