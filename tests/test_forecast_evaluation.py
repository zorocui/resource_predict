from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pandas as pd

from resource_predict.core.forecasting import ForecastResult
from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.fit import fit_one_metric
from resource_predict.pipeline.partial import merge_partial_forecast_items
from resource_predict.settings import settings


def context(methods=None, ensemble=False):
    return WorkerContext(
        test_size=4, future_steps=2,
        active_methods=methods or ["rolling_mean"],
        forecast_config={"enable_ensemble": ensemble, "prophet_routing_enabled": False},
        metric_filter_by_id={}, metric_partial_enabled=False,
        existing_partial_ids=set(), sample_interval_seconds=3600.0,
    )


def constant_forecast(method, train, steps):
    value = {"arima": 0.0, "sarima": 2.0}.get(method, float(train.iloc[-1]))
    index = pd.date_range(train.index[-1], periods=steps + 1, freq="h")[1:]
    return ForecastResult(pd.Series(value, index=index), 0.01)


def series(values):
    return pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values), freq="h"))


def test_outer_labels_do_not_change_selection_or_ensemble_weights():
    ctx = context(["arima", "sarima"], ensemble=True)
    results = []
    with patch("resource_predict.pipeline.fit.forecast_by_method", constant_forecast):
        for tail in ([0.0] * 4, [2.0] * 4):
            full = series([1.0] * 36 + tail)
            results.append(fit_one_metric(full.iloc[:-4], full.iloc[-4:], full, ctx=ctx))
    assert results[0][2] == results[1][2] == "ensemble"
    for result in results:
        assert result[5]["evaluation"]["role"] == "independent_test"
        assert result[1]["ensemble"]["validation_rmse"] == 0.0
        assert result[1]["ensemble"]["rmse"] == 1.0
        np.testing.assert_allclose(result[0]["ensemble"], 1.0)


def test_online_forecast_absorbs_latest_observations_even_with_legacy_reuse():
    full = series([0.2] * 36 + [0.8] * 4)
    ctx = context()
    ctx.forecast_config["reuse_backtest_model_for_future"] = True
    with patch("resource_predict.pipeline.fit.forecast_by_method", constant_forecast):
        pred, _, best, future, _, diagnostics = fit_one_metric(
            full.iloc[:-4], full.iloc[-4:], full, ctx=ctx)
    np.testing.assert_allclose(pred[best], 0.2)
    np.testing.assert_allclose(future[best], 0.8)
    provenance = diagnostics["provenance"]
    assert provenance["train_end_ms"] == full.index[-1].value // 1_000_000
    assert provenance["actual_future_methods"][best] == best
    assert diagnostics["reuse_backtest_model_for_future"] is False


def test_failed_future_uses_honest_baseline_identity():
    full = series([0.2] * 40)
    def forecast(method, train, steps):
        if method == "arima" and len(train) == 40:
            raise ValueError("online failed")
        return constant_forecast(method, train, steps)
    with patch("resource_predict.pipeline.fit.forecast_by_method", forecast):
        _, _, best, future, _, diagnostics = fit_one_metric(
            full.iloc[:-4], full.iloc[-4:], full, ctx=context(["arima"]))
    assert best == "rolling_mean"
    assert "arima" not in future
    assert diagnostics["routing"]["selected_method"] == "arima"
    assert diagnostics["provenance"]["actual_future_methods"][best] == "rolling_mean"


def test_short_history_does_not_select_using_test_scores():
    full = series([1.0] * 4 + [2.0] * 4)
    with patch("resource_predict.pipeline.fit.forecast_by_method", constant_forecast):
        _, _, best, _, _, diagnostics = fit_one_metric(
            full.iloc[:-4], full.iloc[-4:], full, ctx=context(["arima", "sarima"]))
    assert best == "arima"
    assert diagnostics["evaluation"]["selection_status"] == "insufficient_validation_history"


def test_rolling_ensemble_scores_actual_residuals():
    full = series([1.0] * 64)
    cfg = replace(settings.forecast, rolling_backtest_folds=3)
    with patch("resource_predict.pipeline.fit.forecast_by_method", constant_forecast), patch(
        "resource_predict.pipeline.fit.settings", type("Settings", (), {"forecast": cfg})()
    ):
        _, metrics, _, _, _, _ = fit_one_metric(
            full.iloc[:-4], full.iloc[-4:], full, ctx=context(["arima", "sarima"], True))
    assert metrics["ensemble"]["rolling_folds"] == 3
    assert metrics["ensemble"]["rolling_rmse"] == 0
    assert metrics["arima"]["rolling_rmse"] == 1


def test_partial_update_preserves_correct_metric_provenance():
    old = {"resource_id": "vm-1", "forecast_diagnostics": {"cpu": {"origin": "old"},
           "memory": {"origin": "retained"}}, "data_quality": {"cpu": {"level": "poor"}}}
    new = {"resource_id": "vm-1", "forecast_diagnostics": {"cpu": {"origin": "new"}},
           "data_quality": {"cpu": {"level": "good"}}}
    merged = merge_partial_forecast_items([old], [new], metric_names_by_resource={"vm-1": {"cpu"}})[0]
    assert merged["forecast_diagnostics"] == {"cpu": {"origin": "new"}, "memory": {"origin": "retained"}}
    assert merged["data_quality"]["cpu"]["level"] == "good"
