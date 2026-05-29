"""最优预测方法选择。"""
from __future__ import annotations

from typing import Any, Dict


def choose_best_method(
    *,
    metrics_by_method: Dict[str, Dict[str, float]],
    anomaly: Dict[str, Any],
) -> str:
    """从候选方法中选出最优方法。

    正常情况按 selection_rmse 最小；存在异常时优先鲁棒候选
    (ensemble / seasonal_naive / rolling_mean)。
    """
    candidates = list(metrics_by_method.keys())
    if not candidates:
        raise ValueError("no forecast candidates")
    best = min(
        candidates,
        key=lambda k: metrics_by_method[k].get("selection_rmse", metrics_by_method[k].get("rmse", float("inf"))),
    )
    if not anomaly.get("is_anomalous"):
        return best
    robust_candidates = [m for m in ("ensemble", "seasonal_naive", "rolling_mean") if m in metrics_by_method]
    if not robust_candidates:
        return best
    return min(
        robust_candidates,
        key=lambda k: metrics_by_method[k].get("selection_rmse", metrics_by_method[k].get("rmse", float("inf"))),
    )
