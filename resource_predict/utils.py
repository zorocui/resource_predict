"""公共工具函数：数值解析、策略分级、统计计算。

消除 core/、services/ 之间的重复定义。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

import numpy as np


def parse_positive_finite(value: Any) -> Optional[float]:
    """严格正有限浮点数解析：float > 0 且 isfinite，否则返回 None。"""
    try:
        v = float(value)
    except Exception:
        return None
    return v if np.isfinite(v) and v > 0 else None


def parse_float_or_none(value: Any) -> Optional[float]:
    """宽松浮点数解析：仅做 float() 转换，失败返回 None。"""
    try:
        return float(value)
    except Exception:
        return None


def parse_positive_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """解析为正整数；失败或非正数时返回 default。"""
    try:
        parsed = int(float(value))
    except Exception:
        return default
    return parsed if parsed > 0 else default


def resolve_policy_tier(
    spec: Dict[str, Any],
    *,
    fields: Sequence[str],
    cfg: Any = None,
) -> str:
    """根据 spec 中的显式 policy_tier 字段或文本匹配判定策略分级。

    Parameters
    ----------
    spec : dict
        资源规格字典。
    fields : sequence of str
        参与文本匹配的 spec key 列表。
    cfg : optional
        DecisionConfig 对象；为 None 时从 settings.decision 读取。
    """
    if cfg is None:
        from resource_predict.settings import settings

        cfg = settings.decision
    explicit = str((spec or {}).get("policy_tier") or "").lower().strip()
    if explicit in {"conservative", "balanced", "aggressive"}:
        return explicit
    text = " ".join(
        str((spec or {}).get(k) or "").lower() for k in fields
    )
    if any(x and x in text for x in cfg.conservative_namespaces):
        return "conservative"
    if any(x and x in text for x in cfg.aggressive_namespaces):
        return "aggressive"
    default_tier = str(cfg.default_policy_tier or "balanced").lower().strip()
    return default_tier if default_tier in {"conservative", "balanced", "aggressive"} else "balanced"


def compute_metric_stats(
    values: np.ndarray,
    *,
    extended: bool = False,
    filter_nonfinite: bool = True,
) -> Dict[str, float]:
    """计算指标统计量。

    Parameters
    ----------
    values : array-like
        数值数组。
    extended : bool
        False 返回 {avg, p95, peak}；True 额外返回 {valley, gap, std}。
    filter_nonfinite : bool
        为 True 时先用 np.isfinite 过滤再计算。
    """
    arr = np.asarray(values, dtype=float)
    if filter_nonfinite:
        arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        base: Dict[str, float] = {"avg": 0.0, "p95": 0.0, "peak": 0.0}
        if extended:
            base.update(valley=0.0, gap=0.0, std=0.0)
        return base
    peak = float(np.max(arr))
    result: Dict[str, float] = {
        "avg": float(np.mean(arr)),
        "p95": float(np.percentile(arr, 95)),
        "peak": peak,
    }
    if extended:
        valley = float(np.min(arr))
        result["valley"] = valley
        result["gap"] = peak - valley
        result["std"] = float(np.std(arr))
    return result


def require_float(
    value: Any,
    name: str,
    *,
    error_cls: type = ValueError,
) -> float:
    """必填浮点数解析：失败时抛出 error_cls。"""
    result = parse_float_or_none(value)
    if result is None:
        raise error_cls(f"missing {name}")
    return result
