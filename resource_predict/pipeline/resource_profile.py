"""资源画像构建。"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np


def build_resource_profile(
    *,
    resource_type: str,
    futures_by_metric: Dict[str, np.ndarray],
    advice: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """基于未来窗口预测 P95 构建资源负载画像。"""
    metric_scores: Dict[str, float] = {}
    for metric, values in futures_by_metric.items():
        arr = np.asarray(values, dtype=float)
        arr = arr[np.isfinite(arr)]
        metric_scores[metric] = float(np.percentile(arr, 95)) if arr.size else 0.0
    hot = [m for m, v in metric_scores.items() if v >= 0.8]
    cold = [m for m, v in metric_scores.items() if v <= 0.2]
    dominant = max(metric_scores, key=metric_scores.get) if metric_scores else ""
    if len(hot) >= 2:
        shape = "balanced_pressure"
    elif dominant == "cpu" and hot:
        shape = "compute_bound"
    elif dominant == "memory" and hot:
        shape = "memory_bound"
    elif dominant == "disk" and hot:
        shape = "storage_bound"
    elif len(cold) == len(metric_scores) and metric_scores:
        shape = "idle_candidate"
    else:
        shape = "steady"
    metric_actions: Dict[str, str] = {}
    if isinstance(advice, dict) and isinstance(advice.get("metric_actions"), dict):
        metric_actions = advice["metric_actions"]
    return {
        "resource_type": resource_type,
        "shape": shape,
        "dominant_metric": dominant,
        "hot_metrics": hot,
        "cold_metrics": cold,
        "metric_p95": {k: round(v, 4) for k, v in metric_scores.items()},
        "metric_actions": metric_actions,
    }
