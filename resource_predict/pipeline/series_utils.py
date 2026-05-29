"""序列转换与基础指标计算工具。"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd


def to_ms(index: pd.DatetimeIndex) -> List[int]:
    """将 DatetimeIndex 转为毫秒整数列表。"""
    return (index.view("int64") // 1_000_000).tolist()


def series_to_lists(s: pd.Series) -> List[float]:
    """将 pd.Series 转为 float 列表。"""
    return s.to_numpy(dtype=float).tolist()


def compute_metrics(y_true: pd.Series, y_pred: pd.Series) -> Dict[str, float]:
    """计算 MAE / RMSE。"""
    yt = y_true.to_numpy(dtype=float)
    yp = y_pred.to_numpy(dtype=float)
    mae = float(np.mean(np.abs(yt - yp)))
    rmse = float(np.sqrt(np.mean((yt - yp) ** 2)))
    return {"mae": mae, "rmse": rmse}
