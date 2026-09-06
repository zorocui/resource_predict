"""Independent holdout evaluation and fresh online forecasts."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.anomaly import anomaly_profile
from resource_predict.pipeline.forecasting import ensemble_series, forecast_by_method
from resource_predict.pipeline.metrics import validation_backtest_metrics
from resource_predict.pipeline.model_selection import choose_best_method
from resource_predict.pipeline.prophet_routing import prophet_routing_decision
from resource_predict.pipeline.series_utils import compute_metrics
from resource_predict.settings import settings

logger = logging.getLogger(__name__)
MODEL_VERSION = "forecast-baseline-v2"


def canonical_future_index(
    test_index: pd.DatetimeIndex,
    steps: int,
    sample_interval_seconds: Optional[float],
) -> pd.DatetimeIndex:
    """Build the shared future timeline immediately after the real endpoint."""
    if not isinstance(test_index, pd.DatetimeIndex) or test_index.empty:
        raise ValueError("cannot build future index without a test endpoint")
    seconds = float(sample_interval_seconds or 0)
    if not np.isfinite(seconds) or seconds <= 0:
        raise ValueError("cannot build future index without a positive sample interval")
    return pd.date_range(test_index.max() + pd.Timedelta(seconds=seconds),
                         periods=int(steps), freq=pd.Timedelta(seconds=seconds))


def fit_one_metric(
    y_train: pd.Series,
    y_test: pd.Series,
    y_full: pd.Series,
    *,
    ctx: WorkerContext,
) -> tuple[Dict[str, pd.Series], Dict[str, Dict[str, float]], str, Dict[str, pd.Series], Dict[str, float], Dict[str, Any]]:
    """Select on training-only validation, test once, refit on current data.

    Insufficient validation history uses a predefined candidate priority. The
    outer holdout scores never choose models or set ensemble weights.
    """
    cfg = settings.forecast
    timing = {method: 0.0 for method in ctx.active_methods}
    failures: Dict[str, str] = {}
    phase_failures: Dict[str, Dict[str, str]] = {}
    started_ms = int(time.time() * 1000)
    future_index = canonical_future_index(y_full.index, ctx.future_steps, ctx.sample_interval_seconds)

    def predict(method, history, index, phase):
        started = time.perf_counter()
        try:
            result = forecast_by_method(method, history, len(index))
            pred = result.yhat.copy()
            if len(pred) != len(index) or not np.isfinite(pred.to_numpy(dtype=float)).all():
                raise ValueError("forecast length mismatch or non-finite prediction")
            pred.index = index
            return pred
        except Exception as exc:
            failures[method] = str(exc)
            phase_failures.setdefault(phase, {})[method] = str(exc)
            logger.warning("[forecast] %s failed during %s: %s", method, phase, exc)
            return None
        finally:
            timing[method] = timing.get(method, 0.0) + time.perf_counter() - started

    # Routing sees only data preceding validation as well as the independent test.
    min_train = max(ctx.test_size, 24)
    fold_count = min(max(1, int(cfg.rolling_backtest_folds)),
                     max(0, (len(y_train) - min_train) // ctx.test_size))
    route_history = y_train.iloc[:len(y_train) - fold_count * ctx.test_size] if fold_count else y_train
    anom = anomaly_profile(route_history, zscore_threshold=float(cfg.anomaly_route_zscore_threshold))
    routing = prophet_routing_decision(
        route_history, active_methods=ctx.active_methods, anomaly=anom,
        enabled=bool(ctx.forecast_config.get("prophet_routing_enabled", False)),
        mode=str(ctx.forecast_config.get("prophet_routing_mode", "auto")),
    )
    methods = [m for m in ctx.active_methods if not (m == "prophet" and routing.get("decision") == "skipped")]
    methods = methods or list(ctx.active_methods) or ["rolling_mean"]
    enable_ensemble = bool(ctx.forecast_config.get("enable_ensemble", False))
    validation, evaluation = validation_backtest_metrics(
        y_train, methods, test_size=ctx.test_size, folds=int(cfg.rolling_backtest_folds),
        enable_ensemble=enable_ensemble, predict=predict,
    )
    if validation:
        selected = choose_best_method(metrics_by_method=validation, anomaly=anom)
        selection_status = "validated"
    else:
        selected = next((m for m in ("seasonal_naive", "rolling_mean") if m in methods), methods[0])
        selection_status = "insufficient_validation_history" if not fold_count else "validation_failed"

    weights = {m: validation[m] for m in methods if m in validation}
    ensemble_members = list(weights)
    can_ensemble = enable_ensemble and "ensemble" in validation and len(ensemble_members) > 1
    preds: Dict[str, pd.Series] = {}
    for method in methods:
        pred = predict(method, y_train, y_test.index, "test")
        if pred is not None:
            preds[method] = pred
    if can_ensemble and all(m in preds for m in ensemble_members):
        pred = ensemble_series({m: preds[m] for m in ensemble_members}, weights, enable_ensemble=True)
        if pred is not None:
            preds["ensemble"] = pred

    # Online refits include the latest holdout observations, even for legacy configs.
    future: Dict[str, pd.Series] = {}
    for method in methods:
        pred = predict(method, y_full, future_index, "future")
        if pred is not None:
            future[method] = pred
    if can_ensemble and all(m in future for m in ensemble_members):
        pred = ensemble_series({m: future[m] for m in ensemble_members}, weights, enable_ensemble=True)
        if pred is not None:
            future["ensemble"] = pred
    best = selected
    if selected not in future:
        best = "rolling_mean"
        failures.setdefault(selected, "selected future unavailable; rolling_mean used")
        if best not in future:
            pred = predict(best, y_full, future_index, "future_fallback")
            if pred is not None:
                future[best] = pred
    if best not in future:
        raise RuntimeError("no valid future forecast available")
    # Include honest baseline test evidence after a runtime fallback, without
    # attributing its error to the failed selected model.
    if best not in preds:
        pred = predict(best, y_train, y_test.index, "test_fallback")
        if pred is not None:
            preds[best] = pred
    metrics = {m: {**compute_metrics(y_test, pred), **validation.get(m, {})}
               for m, pred in preds.items()}
    configuration = {"algorithm": asdict(cfg), "runtime": ctx.forecast_config,
                     "active_methods": ctx.active_methods, "test_size": ctx.test_size,
                     "future_steps": ctx.future_steps, "sample_interval_seconds": ctx.sample_interval_seconds}
    config_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    evaluation.update(
        role="independent_test", selection_status=selection_status,
        selected_method=selected, test_start_ms=int(y_test.index[0].value // 1_000_000),
        test_end_ms=int(y_test.index[-1].value // 1_000_000),
        test_train_end_ms=int(y_train.index[-1].value // 1_000_000),
        routing_train_end_ms=int(route_history.index[-1].value // 1_000_000),
        ensemble_weight_scores={m: weights[m]["selection_rmse"] for m in ensemble_members},
        validation_metrics=validation,
    )
    diagnostics = {
        "anomaly_profile": anom,
        "routing": {"selected_method": selected, "actual_future_method": best,
                    "route": anom.get("route", "normal"), "reason": selection_status},
        "prophet_routing": routing, "reuse_backtest_model_for_future": False,
        "legacy_reuse_requested": bool(ctx.forecast_config.get("reuse_backtest_model_for_future", False)),
        "method_failures": failures, "phase_failures": phase_failures,
        "evaluation": evaluation,
        "provenance": {
            "generated_at_epoch_ms": started_ms,
            "data_end_ms": int(y_full.index[-1].value // 1_000_000),
            "train_end_ms": int(y_full.index[-1].value // 1_000_000),
            "forecast_start_ms": int(future_index[0].value // 1_000_000),
            "forecast_end_ms": int(future_index[-1].value // 1_000_000),
            "model_version": MODEL_VERSION, "config_hash": config_hash,
            "actual_future_methods": {m: m for m in future},
            "ensemble_members": ensemble_members if "ensemble" in future else [],
        },
    }
    return preds, metrics, best, future, timing, diagnostics
