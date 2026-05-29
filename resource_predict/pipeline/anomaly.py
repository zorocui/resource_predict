"""异常检测与路由。"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd


def anomaly_profile(y_full: pd.Series, *, zscore_threshold: float) -> Dict[str, Any]:
    """基于鲁棒 z-score (MAD) 检测序列尾部异常。"""
    values = y_full.to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 8:
        return {
            "is_anomalous": False,
            "robust_zscore": 0.0,
            "recent_value": float(values[-1]) if values.size else 0.0,
            "route": "normal",
        }
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    scale = max(1.4826 * mad, 1e-6)
    recent_window = values[-min(3, values.size):]
    recent_value = float(np.max(recent_window))
    robust_z = float(abs(recent_value - median) / scale)
    return {
        "is_anomalous": bool(robust_z >= zscore_threshold),
        "robust_zscore": round(robust_z, 3),
        "recent_value": recent_value,
        "median": median,
        "mad": mad,
        "route": "robust" if robust_z >= zscore_threshold else "normal",
    }
