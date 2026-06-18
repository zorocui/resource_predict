from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping

from resource_predict.data.io import atomic_write_json

logger = logging.getLogger(__name__)

ACTION_GATE_STATE_FILENAME = "action_gate_state.json"
ACTION_GATE_STATE_SCHEMA_VERSION = 1

_SCALE_OUT_ACTIONS = {"scale_out", "scale_out_candidate"}
_SCALE_IN_ACTIONS = {"scale_in", "scale_in_candidate"}


def load_action_gate_state(out_base: Path) -> Dict[str, Any]:
    path = out_base / ACTION_GATE_STATE_FILENAME
    if not path.exists():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[action_gate] 无法读取确认状态账本 %s，将从空状态恢复: %s", path, exc)
        return _empty_state()
    if not isinstance(payload, dict) or payload.get("schema_version") != ACTION_GATE_STATE_SCHEMA_VERSION:
        logger.warning("[action_gate] 确认状态账本版本无效，将从空状态恢复: %s", path)
        return _empty_state()
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        logger.warning("[action_gate] 确认状态账本 resources 无效，将从空状态恢复: %s", path)
        return _empty_state()
    return {
        "schema_version": ACTION_GATE_STATE_SCHEMA_VERSION,
        "resources": {str(key): value for key, value in resources.items() if isinstance(value, dict)},
    }


def apply_action_gate_confirmations(
    resources_items: Iterable[MutableMapping[str, Any]],
    *,
    eligible_resource_ids: Iterable[str],
    prior_state: Mapping[str, Any],
    retention_days: int,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Apply one successful prediction round and return the proposed persisted state."""
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)
    eligible = {str(resource_id) for resource_id in eligible_resource_ids}
    records = _retained_records(prior_state, current_time, retention_days)
    timestamp = current_time.isoformat(timespec="seconds").replace("+00:00", "Z")

    for item in resources_items:
        resource_id = str(item.get("resource_id") or "")
        if not resource_id or resource_id not in eligible:
            continue
        advice = item.get("scaling_advice")
        if not isinstance(advice, dict):
            records.pop(resource_id, None)
            continue
        gate = advice.get("action_gate")
        if not isinstance(gate, dict):
            records.pop(resource_id, None)
            continue

        direction = normalize_action_direction(advice.get("action"))
        if direction is None:
            records.pop(resource_id, None)
            gate["observed_consistent_rounds"] = 0
            continue

        previous = records.get(resource_id, {})
        previous_direction = str(previous.get("action_direction") or "")
        previous_rounds = _positive_int(previous.get("consistent_rounds"), default=0)
        observed = previous_rounds + 1 if previous_direction == direction else 1
        required = max(1, _positive_int(gate.get("required_consistent_rounds"), default=1))
        observed = min(observed, required)
        ready = observed >= required

        records[resource_id] = {
            "action_direction": direction,
            "consistent_rounds": observed,
            "last_confirmed_at": timestamp,
        }
        gate["observed_consistent_rounds"] = observed
        gate["state"] = "ready" if ready else "observe"
        gate["reason"] = (
            f"confirmed {observed}/{required} consistent rounds; ready for execution review"
            if ready
            else f"confirmed {observed}/{required} consistent rounds; needs repeated confirmation"
        )

    return {
        "schema_version": ACTION_GATE_STATE_SCHEMA_VERSION,
        "resources": records,
    }


def write_action_gate_state(out_base: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_json(
        out_base / ACTION_GATE_STATE_FILENAME,
        dict(payload),
        ensure_ascii=False,
        indent=2,
    )


def normalize_action_direction(action: Any) -> str | None:
    normalized = str(action or "").strip().lower()
    if normalized in _SCALE_OUT_ACTIONS:
        return "scale_out"
    if normalized in _SCALE_IN_ACTIONS:
        return "scale_in"
    return None


def _empty_state() -> Dict[str, Any]:
    return {"schema_version": ACTION_GATE_STATE_SCHEMA_VERSION, "resources": {}}


def _retained_records(
    prior_state: Mapping[str, Any],
    now: datetime,
    retention_days: int,
) -> Dict[str, Dict[str, Any]]:
    resources = prior_state.get("resources", {}) if isinstance(prior_state, Mapping) else {}
    if not isinstance(resources, Mapping):
        return {}
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    retained: Dict[str, Dict[str, Any]] = {}
    for resource_id, value in resources.items():
        if not isinstance(value, Mapping):
            continue
        confirmed_at = _parse_timestamp(value.get("last_confirmed_at"))
        if confirmed_at is None or confirmed_at < cutoff:
            continue
        direction = normalize_action_direction(value.get("action_direction"))
        rounds = _positive_int(value.get("consistent_rounds"), default=0)
        if direction is None or rounds <= 0:
            continue
        retained[str(resource_id)] = {
            "action_direction": direction,
            "consistent_rounds": rounds,
            "last_confirmed_at": confirmed_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
    return retained


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
