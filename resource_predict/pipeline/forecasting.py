"""预测方法调度与集成融合。"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from resource_predict.core.forecasting import (
    forecast_arima,
    forecast_prophet,
    forecast_rolling_mean,
    forecast_sarima,
    forecast_seasonal_naive,
)


def forecast_by_method(method_name: str, y_train: pd.Series, steps: int):
    """根据方法名调度对应预测函数。"""
    if method_name == "arima":
        return forecast_arima(y_train, steps)
    if method_name == "sarima":
        return forecast_sarima(y_train, steps)
    if method_name == "prophet":
        return forecast_prophet(y_train, steps)
    if method_name == "seasonal_naive":
        return forecast_seasonal_naive(y_train, steps)
    if method_name == "rolling_mean":
        return forecast_rolling_mean(y_train, steps)
    raise ValueError(f"Unsupported forecast method: {method_name}")


def ensemble_series(
    preds_by_method: Dict[str, pd.Series],
    metrics_by_method: Dict[str, Dict[str, float]],
    *,
    enable_ensemble: bool,
) -> Optional[pd.Series]:
    """按 RMSE 倒数加权融合多方法预测。"""
    if not preds_by_method or not enable_ensemble:
        return None
    weighted_values = None
    total_weight = 0.0
    index = next(iter(preds_by_method.values())).index
    for method_name, series in preds_by_method.items():
        score = float(metrics_by_method.get(method_name, {}).get("selection_rmse", np.nan))
        if not np.isfinite(score):
            score = float(metrics_by_method.get(method_name, {}).get("rmse", np.nan))
        if not np.isfinite(score):
            continue
        weight = 1.0 / max(score, 1e-6)
        arr = series.to_numpy(dtype=float)
        weighted_values = arr * weight if weighted_values is None else weighted_values + arr * weight
        total_weight += weight
    if weighted_values is None or total_weight <= 0:
        return None
    return pd.Series(weighted_values / total_weight, index=index, name="yhat")
