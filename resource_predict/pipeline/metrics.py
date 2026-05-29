"""回测指标与均值统计。"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from resource_predict.pipeline.forecasting import forecast_by_method
from resource_predict.pipeline.series_utils import compute_metrics


def rolling_backtest_metrics(
    y_full: pd.Series,
    method_name: str,
    *,
    test_size: int,
    folds: int = 1,
) -> Dict[str, float]:
    """滚动回测计算 rolling_mae / rolling_rmse / rolling_folds。"""
    fold_size = int(test_size)
    min_train = max(fold_size, 24)
    if folds <= 1 or len(y_full) < min_train + fold_size * 2:
        return {}
    scores: List[Dict[str, float]] = []
    max_folds = min(folds, max(1, (len(y_full) - min_train) // fold_size))
    for fold in range(max_folds):
        test_end = len(y_full) - fold * fold_size
        test_start = test_end - fold_size
        if test_start < min_train:
            break
        train = y_full.iloc[:test_start]
        test = y_full.iloc[test_start:test_end]
        if len(train) <= 1 or len(test) != fold_size:
            continue
        try:
            res = forecast_by_method(method_name, train, fold_size)
            pred = res.yhat.copy()
            pred.index = test.index
            scores.append(compute_metrics(test, pred))
        except Exception:
            continue
    if not scores:
        return {}
    return {
        "rolling_mae": float(np.mean([s["mae"] for s in scores])),
        "rolling_rmse": float(np.mean([s["rmse"] for s in scores])),
        "rolling_folds": float(len(scores)),
    }


def mean_metric(metrics_by_method: Dict[str, Dict[str, float]], name: str) -> Optional[float]:
    """计算非 ensemble 方法在某指标上的均值。"""
    values = [
        float(v[name])
        for k, v in metrics_by_method.items()
        if k != "ensemble" and v.get(name) is not None
    ]
    return float(np.mean(values)) if values else None
