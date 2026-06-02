from __future__ import annotations

from typing import Any, Dict, Iterable

import numpy as np
import pandas as pd

from resource_predict.core.forecasting import infer_steps_per_day


def prophet_routing_decision(
    y_full: pd.Series,
    *,
    active_methods: Iterable[str],
    anomaly: Dict[str, Any],
    enabled: bool,
    mode: str,
) -> Dict[str, Any]:
    """判断当前序列是否值得运行 Prophet。"""
    methods = set(active_methods)
    if "prophet" not in methods:
        return {"enabled": enabled, "decision": "not_configured", "reason": "prophet_not_enabled"}

    mode = (mode or "auto").strip().lower()
    if mode == "always" or not enabled:
        return {"enabled": enabled, "decision": "run", "reason": "routing_disabled_or_always"}
    if mode == "never" and _has_fallback(methods):
        return {"enabled": enabled, "decision": "skipped", "reason": "mode_never"}
    if not _has_fallback(methods):
        return {"enabled": enabled, "decision": "run", "reason": "no_fallback_method"}

    values = y_full.to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 48:
        return {
            "enabled": enabled,
            "decision": "skipped",
            "reason": "short_series",
            "points": int(values.size),
        }

    if anomaly.get("is_anomalous"):
        return {
            "enabled": enabled,
            "decision": "skipped",
            "reason": "recent_anomaly_prefers_robust_candidate",
            "robust_zscore": anomaly.get("robust_zscore"),
        }

    mean_abs = float(np.mean(np.abs(values)))
    std = float(np.std(values))
    value_range = float(np.max(values) - np.min(values))
    cv = std / max(mean_abs, 1e-6)
    if std < 0.005 or value_range < 0.02 or cv < 0.03:
        return {
            "enabled": enabled,
            "decision": "skipped",
            "reason": "low_variance_stable_series",
            "std": round(std, 6),
            "range": round(value_range, 6),
            "cv": round(cv, 6),
        }

    trend_strength = _trend_strength(values)
    seasonality_strength = _seasonality_strength(y_full, values)
    if trend_strength >= 0.18 or seasonality_strength >= 0.25:
        return {
            "enabled": enabled,
            "decision": "run",
            "reason": "trend_or_seasonality_detected",
            "trend_strength": round(trend_strength, 6),
            "seasonality_strength": round(seasonality_strength, 6),
        }

    return {
        "enabled": enabled,
        "decision": "skipped",
        "reason": "simple_series_covered_by_fast_methods",
        "trend_strength": round(trend_strength, 6),
        "seasonality_strength": round(seasonality_strength, 6),
    }


def _has_fallback(methods: set[str]) -> bool:
    return bool(methods.intersection({"seasonal_naive", "rolling_mean", "arima", "sarima"}))


def _trend_strength(values: np.ndarray) -> float:
    if values.size < 3:
        return 0.0
    x = np.arange(values.size, dtype=float)
    slope = float(np.polyfit(x, values, 1)[0])
    return abs(slope) * values.size / max(float(np.std(values)), 1e-6)


def _seasonality_strength(y_full: pd.Series, values: np.ndarray) -> float:
    if not isinstance(y_full.index, pd.DatetimeIndex) or values.size < 8:
        return 0.0
    period = infer_steps_per_day(y_full.index)
    if period <= 1 or values.size < period * 2:
        return 0.0
    usable = values[-(values.size // period) * period:]
    if usable.size < period * 2:
        return 0.0
    matrix = usable.reshape((-1, period))
    seasonal_profile = np.mean(matrix, axis=0)
    return float(np.std(seasonal_profile) / max(np.std(usable), 1e-6))
