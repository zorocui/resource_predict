from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def apply_display_window(
    detail: Dict[str, Any],
    *,
    test_size: int,
    display_window_points: int,
) -> None:
    """仅截短图表 y_train，不改变 raw_data.json 存储。"""
    if display_window_points <= 0:
        return
    max_train = max(0, display_window_points - test_size)
    if max_train <= 0 and display_window_points > 0:
        logger.warning(
            "display_window_points (%d) < test_size (%d); chart training data will be hidden",
            display_window_points,
            test_size,
        )
    charts = detail.get("charts")
    if not isinstance(charts, dict):
        return
    for metric in ("cpu", "memory", "disk"):
        block = charts.get(metric)
        if not isinstance(block, dict):
            continue
        y_train = block.get("y_train", [])
        x_train_ms = block.get("x_train_ms", [])
        if not isinstance(y_train, list) or not isinstance(x_train_ms, list):
            continue
        if len(y_train) <= max_train:
            continue
        block["y_train"] = y_train[-max_train:]
        block["x_train_ms"] = x_train_ms[-max_train:]
