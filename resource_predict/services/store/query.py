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
    if action in {"scale_out", "scale_out_candidate"}:
        return 2
    if action in {"scale_in", "scale_in_candidate"}:
        return 1
    return 0


def matches_query(item: Dict[str, Any], q: str) -> bool:
    """按资源名、ID、IP、集群匹配搜索词。"""
    if not q:
        return True
    qv = q.lower()
    spec = item.get("spec", {}) if isinstance(item, dict) else {}
    ip = ""
    cluster = ""
    if isinstance(spec, dict):
        ip = str(spec.get("ip", ""))
        cluster = str(spec.get("cluster", ""))
        candidates_extra = [
            str(spec.get("namespace", "")),
            str(spec.get("workload_kind", "")),
            str(spec.get("workload_name", "")),
            str(spec.get("pod", "")),
            str(spec.get("container", "")),
            str(spec.get("node", "")),
            str(spec.get("owner_kind", "")),
            str(spec.get("owner_name", "")),
        ]
    else:
        candidates_extra = []

    candidates = [
        str(item.get("resource_id", "")),
        str(item.get("resource_name", "")),
        str(item.get("name", "")),
        ip,
        cluster,
        *candidates_extra,
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
