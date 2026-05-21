from __future__ import annotations

from typing import Any, Dict, Optional

from resource_predict.data.updater import get_update_status


def safe_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def action_priority(item: Dict[str, Any]) -> int:
    advice = item.get("scaling_advice", {}) if isinstance(item, dict) else {}
    if not isinstance(advice, dict):
        return 0
    action = str(advice.get("action", "hold")).lower()
    if action == "scale_out":
        return 2
    if action == "scale_in":
        return 1
    return 0


def matches_query(item: Dict[str, Any], q: str) -> bool:
    """按资源名、ID、IP、集群匹配搜索词。"""
    if not q:
        return True
    qv = q.lower()
    vm_spec = item.get("vm_spec", {}) if isinstance(item, dict) else {}
    ip = ""
    cluster = ""
    if isinstance(vm_spec, dict):
        ip = str(vm_spec.get("ip", ""))
        cluster = str(vm_spec.get("cluster", ""))

    candidates = [
        str(item.get("resource_id", "")),
        str(item.get("resource_name", "")),
        str(item.get("name", "")),
        ip,
        cluster,
    ]
    return any(qv in c.lower() for c in candidates if c)


def prediction_pending_for(resource_id: str) -> Optional[Dict[str, Any]]:
    status = get_update_status()
    if not status.get("running"):
        return None
    phase = str(status.get("phase") or "")
    if phase not in {"writing_raw", "predicting"}:
        return None
    ids = {str(x) for x in (status.get("current_resource_ids") or [])}
    if str(resource_id) not in ids:
        return None
    return status
