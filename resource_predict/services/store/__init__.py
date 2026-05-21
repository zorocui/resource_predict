"""
Web 层读取 outputs/ 预测产物：摘要、详情合并、列表查询辅助。

对外公开 API（供 app 与路由使用）：
"""

from resource_predict.services.store.forecast_store import ForecastStore
from resource_predict.services.store.query import (
    action_priority,
    matches_query,
    prediction_pending_for,
    safe_int,
)

__all__ = [
    "ForecastStore",
    "action_priority",
    "matches_query",
    "prediction_pending_for",
    "safe_int",
]
