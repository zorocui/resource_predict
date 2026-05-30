from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from resource_predict.resource_types import resource_type_of


@dataclass(frozen=True)
class ForecastWindow:
    resource_family: str
    test_size: int
    future_steps: int
    sample_interval_seconds: Optional[float]
    test_duration: Optional[str]
    future_duration: Optional[str]
    source: str


def resource_family_for_items(items: list[dict[str, Any]]) -> str:
    families = {_resource_family(item) for item in items}
    if len(families) == 1:
        return next(iter(families))
    if "workload" in families and "vm" not in families:
        return "workload"
    return "vm"


def infer_series_freq(index: pd.DatetimeIndex) -> str:
    try:
        freq = pd.infer_freq(index)
    except ValueError:
        freq = None
    if freq:
        return freq
    seconds = infer_sample_interval_seconds(index)
    if seconds is None:
        return "h"
    return _seconds_to_freq(seconds)


def infer_sample_interval_seconds(index: pd.DatetimeIndex) -> Optional[float]:
    if len(index) < 2:
        return None
    diffs = np.diff(index.sort_values().view("int64")) / 1_000_000_000
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if diffs.size == 0:
        return None
    return float(np.median(diffs))


def resolve_forecast_window(
    *,
    cfg: Any,
    items: list[dict[str, Any]],
    explicit_test_size: Optional[int],
    explicit_future_steps: Optional[int],
) -> ForecastWindow:
    if not items:
        raise ValueError("无法解析预测窗口：资源列表为空")

    family = resource_family_for_items(items)
    index = _first_metric_index(items)
    sample_seconds = infer_sample_interval_seconds(index)

    test_duration = _scoped_value(cfg, family, "test_duration")
    future_duration = _scoped_value(cfg, family, "future_duration")

    if explicit_test_size is not None:
        test_size = int(explicit_test_size)
        test_duration = None
        test_source = "argument"
    else:
        test_size_value = _scoped_value(cfg, family, "test_size")
        if test_duration:
            test_size = _steps_for_duration(test_duration, sample_seconds)
            test_source = f"{family}_test_duration"
        else:
            test_size = int(
                test_size_value
                if test_size_value is not None
                else _default_value(cfg, "test_size")
            )
            test_source = f"{family}_test_size" if test_size_value is not None else "default_test_size"

    if explicit_future_steps is not None:
        future_steps = int(explicit_future_steps)
        future_duration = None
        future_source = "argument"
    else:
        future_steps_value = _scoped_value(cfg, family, "future_steps")
        if future_duration:
            future_steps = _steps_for_duration(future_duration, sample_seconds)
            future_source = f"{family}_future_duration"
        else:
            future_steps = int(
                future_steps_value
                if future_steps_value is not None
                else _default_value(cfg, "future_steps")
            )
            future_source = (
                f"{family}_future_steps" if future_steps_value is not None else "default_future_steps"
            )

    if test_size <= 0:
        raise ValueError("test_size 必须为正整数")
    if future_steps <= 0:
        raise ValueError("future_steps 必须为正整数")

    return ForecastWindow(
        resource_family=family,
        test_size=test_size,
        future_steps=future_steps,
        sample_interval_seconds=sample_seconds,
        test_duration=test_duration,
        future_duration=future_duration,
        source=f"{test_source},{future_source}",
    )


def _resource_family(item: dict[str, Any]) -> str:
    rtype = resource_type_of(item)
    if rtype == "k8s_workload":
        return "workload"
    return "vm"


def _scoped_value(cfg: Any, family: str, name: str) -> Any:
    return getattr(cfg, f"{family}_{name}", None)


def _default_value(cfg: Any, name: str) -> Any:
    preferred = getattr(cfg, f"default_{name}", None)
    if preferred is not None:
        return preferred
    return getattr(cfg, name)


def _first_metric_index(items: list[dict[str, Any]]) -> pd.DatetimeIndex:
    for item in items:
        for value in item.values():
            if isinstance(value, pd.Series) and isinstance(value.index, pd.DatetimeIndex):
                if not value.empty:
                    return value.index
    raise ValueError("无法解析预测窗口：未找到有效时间序列")


def _steps_for_duration(duration: str, sample_seconds: Optional[float]) -> int:
    if sample_seconds is None or sample_seconds <= 0:
        raise ValueError(f"无法按时长 {duration!r} 换算点数：时间序列频率未知")
    delta = pd.Timedelta(duration)
    seconds = float(delta.total_seconds())
    if seconds <= 0:
        raise ValueError(f"预测窗口时长必须为正数: {duration!r}")
    return max(1, int(round(seconds / sample_seconds)))


def _seconds_to_freq(seconds: float) -> str:
    rounded = max(1, int(round(seconds)))
    if rounded % 86400 == 0:
        days = rounded // 86400
        return "D" if days == 1 else f"{days}D"
    if rounded % 3600 == 0:
        hours = rounded // 3600
        return "h" if hours == 1 else f"{hours}h"
    if rounded % 60 == 0:
        minutes = rounded // 60
        return "min" if minutes == 1 else f"{minutes}min"
    return f"{rounded}s"
