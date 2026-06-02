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
    )


class ForecastOptimizationTest(unittest.TestCase):
    def test_reuse_backtest_model_forecasts_holdout_and_future_once(self):
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

        self.assertEqual(calls, [("rolling_mean", 5, 5)])
        self.assertEqual(list(preds["rolling_mean"].values), [0.0, 1.0, 2.0])
        self.assertEqual(list(future["rolling_mean"].values), [3.0, 4.0])
        self.assertAlmostEqual(timing["rolling_mean"], 0.25)
        self.assertTrue(diagnostics["reuse_backtest_model_for_future"])

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
        self.assertAlmostEqual(timing["rolling_mean"], 0.5)
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

        self.assertEqual(calls, ["rolling_mean"])
        self.assertNotIn("prophet", preds)
        self.assertNotIn("prophet", metrics)
        self.assertNotIn("prophet", future)
        self.assertEqual(diagnostics["prophet_routing"]["decision"], "skipped")


if __name__ == "__main__":
    unittest.main()
