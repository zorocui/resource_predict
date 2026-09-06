from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from resource_predict.core.forecasting import ForecastResult
from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.fit import fit_one_metric


def _series(values: list[float], *, freq: str = "h") -> pd.Series:
    return pd.Series(
        values,
        index=pd.date_range("2026-01-01", periods=len(values), freq=freq),
        name="cpu",
    )


def _ctx(config: dict) -> WorkerContext:
    return WorkerContext(
        test_size=3,
        future_steps=2,
        active_methods=["rolling_mean"],
        forecast_config={
            "enabled_methods": ["rolling_mean"],
            "enable_ensemble": False,
            "prophet_routing_enabled": True,
            "prophet_routing_mode": "auto",
            **config,
        },
        metric_filter_by_id={},
        metric_partial_enabled=False,
        existing_partial_ids=set(),
        sample_interval_seconds=300.0,
        max_interpolation_gap_steps=3,
    )


class ForecastOptimizationTest(unittest.TestCase):
    def test_reused_model_future_index_starts_after_real_test_end(self):
        index = pd.to_datetime([
            "2026-08-01 00:00", "2026-08-01 00:05", "2026-08-01 00:10",
            "2026-08-02 00:00", "2026-08-03 04:00", "2026-08-09 04:00",
        ])
        y_full = pd.Series(range(6), index=index, dtype=float)
        y_train, y_test = y_full.iloc[:-3], y_full.iloc[-3:]

        def fake_forecast(_method, train, steps):
            wrong = pd.date_range(train.index[-1], periods=steps + 1, freq="5min")[1:]
            return ForecastResult(pd.Series(range(steps), index=wrong, dtype=float), 0.1)

        with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
            _preds, _metrics, _best, future, _timing, _diagnostics = fit_one_metric(
                y_train, y_test, y_full,
                ctx=_ctx({"reuse_backtest_model_for_future": True}),
            )

        expected = pd.date_range(
            y_test.index[-1] + pd.Timedelta(minutes=5), periods=2, freq="5min"
        )
        self.assertTrue(future["rolling_mean"].index.equals(expected))

    def test_refitted_model_future_index_starts_after_real_test_end(self):
        index = pd.to_datetime([
            "2026-08-01 00:00", "2026-08-01 00:05", "2026-08-01 00:10",
            "2026-08-02 00:00", "2026-08-03 04:00", "2026-08-09 04:00",
        ])
        y_full = pd.Series(range(6), index=index, dtype=float)
        y_train, y_test = y_full.iloc[:-3], y_full.iloc[-3:]

        def fake_forecast(_method, train, steps):
            wrong = pd.date_range(train.index[-1], periods=steps + 1, freq="5min")[1:]
            return ForecastResult(pd.Series(range(steps), index=wrong, dtype=float), 0.1)

        with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
            _preds, _metrics, _best, future, _timing, _diagnostics = fit_one_metric(
                y_train, y_test, y_full,
                ctx=_ctx({"reuse_backtest_model_for_future": False}),
            )

        expected = pd.date_range(
            y_test.index[-1] + pd.Timedelta(minutes=5), periods=2, freq="5min"
        )
        self.assertTrue(future["rolling_mean"].index.equals(expected))

    def test_legacy_reuse_cannot_skip_latest_observations(self):
        y_full = _series([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
        y_train = y_full.iloc[:-3]
        y_test = y_full.iloc[-3:]
        calls: list[tuple[str, int, int]] = []

        def fake_forecast(method: str, y_train_arg: pd.Series, steps: int) -> ForecastResult:
            calls.append((method, len(y_train_arg), steps))
            idx = pd.date_range(y_train_arg.index[-1], periods=steps + 1, freq="h")[1:]
            return ForecastResult(pd.Series(range(steps), index=idx, dtype=float), seconds=0.25)

        with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
            preds, _metrics, _best, future, timing, diagnostics = fit_one_metric(
                y_train,
                y_test,
                y_full,
                ctx=_ctx({"reuse_backtest_model_for_future": True}),
            )

        self.assertEqual(calls, [("rolling_mean", 5, 3), ("rolling_mean", 8, 2)])
        self.assertEqual(list(preds["rolling_mean"].values), [0.0, 1.0, 2.0])
        self.assertEqual(list(future["rolling_mean"].values), [0.0, 1.0])
        self.assertGreaterEqual(timing["rolling_mean"], 0.0)
        self.assertFalse(diagnostics["reuse_backtest_model_for_future"])
        self.assertTrue(diagnostics["legacy_reuse_requested"])

    def test_disabled_reuse_keeps_separate_future_forecast(self):
        y_full = _series([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
        y_train = y_full.iloc[:-3]
        y_test = y_full.iloc[-3:]
        calls: list[tuple[str, int, int]] = []

        def fake_forecast(method: str, y_train_arg: pd.Series, steps: int) -> ForecastResult:
            calls.append((method, len(y_train_arg), steps))
            idx = pd.date_range(y_train_arg.index[-1], periods=steps + 1, freq="h")[1:]
            return ForecastResult(pd.Series(range(steps), index=idx, dtype=float), seconds=0.25)

        with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
            _preds, _metrics, _best, future, timing, diagnostics = fit_one_metric(
                y_train,
                y_test,
                y_full,
                ctx=_ctx({"reuse_backtest_model_for_future": False}),
            )

        self.assertEqual(calls, [("rolling_mean", 5, 3), ("rolling_mean", 8, 2)])
        self.assertEqual(list(future["rolling_mean"].values), [0.0, 1.0])
        self.assertGreaterEqual(timing["rolling_mean"], 0.0)
        self.assertFalse(diagnostics["reuse_backtest_model_for_future"])

    def test_prophet_routing_skips_stable_series_when_fallback_exists(self):
        y_full = _series([0.2] * 80)
        y_train = y_full.iloc[:-3]
        y_test = y_full.iloc[-3:]
        calls: list[str] = []
        ctx = WorkerContext(
            test_size=3,
            future_steps=2,
            active_methods=["prophet", "rolling_mean"],
            forecast_config={
                "enabled_methods": ["prophet", "rolling_mean"],
                "enable_ensemble": False,
                "reuse_backtest_model_for_future": True,
                "prophet_routing_enabled": True,
                "prophet_routing_mode": "auto",
            },
            metric_filter_by_id={},
            metric_partial_enabled=False,
            existing_partial_ids=set(),
            sample_interval_seconds=3600.0,
        )

        def fake_forecast(method: str, y_train_arg: pd.Series, steps: int) -> ForecastResult:
            calls.append(method)
            idx = pd.date_range(y_train_arg.index[-1], periods=steps + 1, freq="h")[1:]
            return ForecastResult(pd.Series([0.2] * steps, index=idx), seconds=0.1)

        with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
            preds, metrics, _best, future, _timing, diagnostics = fit_one_metric(
                y_train,
                y_test,
                y_full,
                ctx=ctx,
            )

        self.assertEqual(calls, ["rolling_mean"] * 3)
        self.assertNotIn("prophet", preds)
        self.assertNotIn("prophet", metrics)
        self.assertNotIn("prophet", future)
        self.assertEqual(diagnostics["prophet_routing"]["decision"], "skipped")

    def test_failed_method_does_not_abort_metric_when_baseline_succeeds(self):
        y_full = _series([0.1, 0.2, 0.3, 0.4, 0.5], freq="5min")
        y_train = y_full.iloc[:-3]
        y_test = y_full.iloc[-3:]
        ctx = WorkerContext(
            test_size=3,
            future_steps=2,
            active_methods=["arima", "rolling_mean"],
            forecast_config={
                "enabled_methods": ["arima", "rolling_mean"],
                "enable_ensemble": False,
                "reuse_backtest_model_for_future": True,
                "prophet_routing_enabled": False,
                "prophet_routing_mode": "auto",
            },
            metric_filter_by_id={},
            metric_partial_enabled=False,
            existing_partial_ids=set(),
            sample_interval_seconds=300.0,
        )

        def fake_forecast(method: str, y_train_arg: pd.Series, steps: int) -> ForecastResult:
            if method == "arima":
                raise ValueError("Need at least 3 dates to infer frequency")
            idx = pd.date_range(y_train_arg.index[-1], periods=steps + 1, freq="5min")[1:]
            return ForecastResult(pd.Series([0.2] * steps, index=idx), seconds=0.1)

        with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
            preds, metrics, best, future, _timing, diagnostics = fit_one_metric(
                y_train,
                y_test,
                y_full,
                ctx=ctx,
            )

        self.assertEqual(best, "rolling_mean")
        self.assertIn("rolling_mean", preds)
        self.assertIn("rolling_mean", metrics)
        self.assertEqual(len(future["rolling_mean"]), 2)
        expected = pd.date_range(
            y_test.index[-1] + pd.Timedelta(minutes=5), periods=2, freq="5min"
        )
        self.assertTrue(future["rolling_mean"].index.equals(expected))
        self.assertIn("arima", diagnostics["method_failures"])

    def test_all_configured_methods_failed_fallback_uses_canonical_future_index(self):
        y_full = _series([0.1, 0.2, 0.3, 0.4, 0.5], freq="5min")
        y_train, y_test = y_full.iloc[:-3], y_full.iloc[-3:]
        ctx = WorkerContext(
            test_size=3,
            future_steps=2,
            active_methods=["arima"],
            forecast_config={
                "enabled_methods": ["arima"],
                "enable_ensemble": False,
                "reuse_backtest_model_for_future": True,
                "prophet_routing_enabled": False,
                "prophet_routing_mode": "auto",
            },
            metric_filter_by_id={},
            metric_partial_enabled=False,
            existing_partial_ids=set(),
            sample_interval_seconds=300.0,
        )

        def fake_forecast(method: str, train: pd.Series, steps: int) -> ForecastResult:
            if method == "arima":
                raise ValueError("configured model failed")
            wrong = pd.date_range(train.index[-1], periods=steps + 1, freq="h")[1:]
            return ForecastResult(pd.Series([0.2] * steps, index=wrong), seconds=0.1)

        with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
            _preds, _metrics, best, future, _timing, _diagnostics = fit_one_metric(
                y_train, y_test, y_full, ctx=ctx
            )

        expected = pd.date_range(
            y_test.index[-1] + pd.Timedelta(minutes=5), periods=2, freq="5min"
        )
        self.assertEqual(best, "rolling_mean")
        self.assertTrue(future["rolling_mean"].index.equals(expected))


if __name__ == "__main__":
    unittest.main()
