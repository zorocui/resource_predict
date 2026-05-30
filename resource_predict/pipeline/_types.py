"""pipeline 模块共享数据结构。"""
from __future__ import annotations

from typing import Any, Dict


class WorkerContext:
    """传递给 _worker / _fit_one_metric 的只读上下文。"""

    __slots__ = (
        "test_size",
        "future_steps",
        "active_methods",
        "forecast_config",
        "metric_filter_by_id",
        "metric_partial_enabled",
        "existing_partial_ids",
    )

    def __init__(
        self,
        *,
        test_size: int,
        future_steps: int,
        active_methods: list[str],
        forecast_config: Dict[str, Any],
        metric_filter_by_id: Dict[str, Any],
        metric_partial_enabled: bool,
        existing_partial_ids: set[str],
    ) -> None:
        self.test_size = test_size
        self.future_steps = future_steps
        self.active_methods = active_methods
        self.forecast_config = forecast_config
        self.metric_filter_by_id = metric_filter_by_id
        self.metric_partial_enabled = metric_partial_enabled
        self.existing_partial_ids = existing_partial_ids
