"""单指标拟合：回测 + 未来预测 + 集成。"""
from __future__ import annotations

import logging
from typing import Any, Dict

import pandas as pd

from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.anomaly import anomaly_profile
from resource_predict.pipeline.forecasting import ensemble_series, forecast_by_method
from resource_predict.pipeline.metrics import mean_metric, rolling_backtest_metrics
from resource_predict.pipeline.model_selection import choose_best_method
from resource_predict.pipeline.prophet_routing import prophet_routing_decision
from resource_predict.pipeline.series_utils import compute_metrics
from resource_predict.settings import settings

logger = logging.getLogger(__name__)


def fit_one_metric(
    y_train: pd.Series,
    y_test: pd.Series,
    y_full: pd.Series,
    *,
    ctx: WorkerContext,
) -> tuple[Dict[str, pd.Series], Dict[str, Dict[str, float]], str, Dict[str, pd.Series], Dict[str, float], Dict[str, Any]]:
    """对单个指标执行全部预测方法 + 回测 + 集成 + 未来预测。

    返回: (preds, metrics, best, preds_future, timing, diagnostics)
    """
    active_methods = ctx.active_methods
    timing: Dict[str, float] = {m: 0.0 for m in active_methods}
    preds: Dict[str, pd.Series] = {}
    metrics: Dict[str, Dict[str, float]] = {}
    method_failures: Dict[str, str] = {}

    zscore_threshold = float(settings.forecast.anomaly_route_zscore_threshold)
    anom = anomaly_profile(y_full, zscore_threshold=zscore_threshold)

    backtest_folds = max(1, int(getattr(settings.forecast, "rolling_backtest_folds", 1)))
    enable_ensemble = bool(ctx.forecast_config.get("enable_ensemble", False))
    reuse_backtest_model = bool(ctx.forecast_config.get("reuse_backtest_model_for_future", False))
    prophet_routing = prophet_routing_decision(
        y_full,
        active_methods=active_methods,
        anomaly=anom,
        enabled=bool(ctx.forecast_config.get("prophet_routing_enabled", False)),
        mode=str(ctx.forecast_config.get("prophet_routing_mode", "auto")),
    )
    effective_methods = [
        m for m in active_methods
        if not (m == "prophet" and prophet_routing.get("decision") == "skipped")
    ]
    if not effective_methods:
        effective_methods = list(active_methods)

    preds_future: Dict[str, pd.Series] = {}
    for m in effective_methods:
        try:
            if reuse_backtest_model:
                res = forecast_by_method(m, y_train, ctx.test_size + ctx.future_steps)
                pred = res.yhat.iloc[:ctx.test_size].copy()
                future_pred = res.yhat.iloc[ctx.test_size:].copy()
            else:
                res = forecast_by_method(m, y_train, ctx.test_size)
                pred = res.yhat.copy()
                future_pred = pd.Series(dtype=float)
        except Exception as exc:
            method_failures[m] = str(exc)
            logger.warning("[forecast] method %s failed during backtest: %s", m, exc)
            continue
        pred.index = y_test.index
        preds[m] = pred
        metrics[m] = compute_metrics(y_test, pred)
        rolling = rolling_backtest_metrics(y_full, m, test_size=ctx.test_size, folds=backtest_folds)
        metrics[m].update(rolling)
        if rolling.get("rolling_rmse") is not None:
            metrics[m]["selection_rmse"] = (
                0.65 * float(metrics[m]["rmse"])
                + 0.35 * float(rolling["rolling_rmse"])
            )
        else:
            metrics[m]["selection_rmse"] = float(metrics[m]["rmse"])
        timing[m] += float(res.seconds)
        if reuse_backtest_model:
            preds_future[m] = future_pred

    if not preds:
        fallback = "rolling_mean"
        res = forecast_by_method(fallback, y_train, ctx.test_size + ctx.future_steps)
        pred = res.yhat.iloc[:ctx.test_size].copy()
        pred.index = y_test.index
        preds[fallback] = pred
        metrics[fallback] = compute_metrics(y_test, pred)
        metrics[fallback]["selection_rmse"] = float(metrics[fallback]["rmse"])
        timing.setdefault(fallback, 0.0)
        timing[fallback] += float(res.seconds)
        preds_future[fallback] = res.yhat.iloc[ctx.test_size:].copy()
        method_failures[fallback] = "used as fallback after all configured methods failed"

    ensemble_pred = ensemble_series(preds, metrics, enable_ensemble=enable_ensemble)
    if ensemble_pred is not None:
        preds["ensemble"] = ensemble_pred
        metrics["ensemble"] = compute_metrics(y_test, ensemble_pred)
        rolling_rmse = mean_metric(metrics, "rolling_rmse")
        if rolling_rmse is not None:
            metrics["ensemble"]["rolling_rmse"] = rolling_rmse
            rolling_mae = mean_metric(metrics, "rolling_mae")
            if rolling_mae is not None:
                metrics["ensemble"]["rolling_mae"] = rolling_mae
            metrics["ensemble"]["rolling_folds"] = min(
                float(v.get("rolling_folds", 0.0))
                for k, v in metrics.items()
                if k != "ensemble"
            )
            metrics["ensemble"]["selection_rmse"] = (
                0.65 * float(metrics["ensemble"]["rmse"])
                + 0.35 * rolling_rmse
            )
        else:
            metrics["ensemble"]["selection_rmse"] = float(metrics["ensemble"]["rmse"])

    best = choose_best_method(metrics_by_method=metrics, anomaly=anom)

    if not reuse_backtest_model:
        for m in effective_methods:
            if m not in preds:
                continue
            try:
                res = forecast_by_method(m, y_full, ctx.future_steps)
            except Exception as exc:
                method_failures[m] = str(exc)
                logger.warning("[forecast] method %s failed during future forecast: %s", m, exc)
                continue
            preds_future[m] = res.yhat.copy()
            timing[m] += float(res.seconds)
    if best not in preds_future:
        fallback = "rolling_mean"
        res = forecast_by_method(fallback, y_full, ctx.future_steps)
        preds_future[best] = res.yhat.copy()
        timing.setdefault(fallback, 0.0)
        timing[fallback] += float(res.seconds)
        method_failures[best] = method_failures.get(
            best,
            "future forecast unavailable; rolling_mean future used",
        )
    ensemble_future = ensemble_series(preds_future, metrics, enable_ensemble=enable_ensemble)
    if ensemble_future is not None:
        preds_future["ensemble"] = ensemble_future

    diagnostics = {
        "anomaly_profile": anom,
        "routing": {
            "selected_method": best,
            "route": anom.get("route", "normal"),
            "reason": "recent anomaly routed to robust candidate"
            if anom.get("is_anomalous")
            else "normal model selection",
        },
        "prophet_routing": prophet_routing,
        "reuse_backtest_model_for_future": reuse_backtest_model,
        "method_failures": method_failures,
    }
    return preds, metrics, best, preds_future, timing, diagnostics
