from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from resource_predict.data.io import atomic_write_json
from resource_predict.settings import settings


logger = logging.getLogger(__name__)

UPDATE_HISTORY_FILENAME = "update_history.json"
UPDATE_HISTORY_RETENTION = 100
_history_lock = threading.Lock()


def update_history_path(out_dir: Optional[str | Path] = None) -> Path:
    return Path(out_dir or settings.app.out_dir) / UPDATE_HISTORY_FILENAME


def get_update_history(
    limit: int = 20,
    *,
    out_dir: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    path = update_history_path(out_dir)
    with _history_lock:
        records = _read_records(path)
    return records[:limit]


def append_update_history(
    record: Dict[str, Any],
    *,
    out_dir: Optional[str | Path] = None,
) -> bool:
    path = update_history_path(out_dir)
    normalized = _normalize_record(record)
    try:
        with _history_lock:
            records = _read_records(path)
            records.insert(0, normalized)
            payload = {
                "version": 1,
                "records": records[:UPDATE_HISTORY_RETENTION],
            }
            atomic_write_json(path, payload, ensure_ascii=False, indent=2)
        return True
    except Exception:
        logger.exception("[update_history] failed to persist update history: %s", path)
        return False


def _read_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[update_history] failed to read update history: %s", path)
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        logger.error("[update_history] invalid history payload: %s", path)
        return []
    valid = [dict(item) for item in records if isinstance(item, dict)]
    valid.sort(key=lambda item: float(item.get("finished_at") or 0), reverse=True)
    return valid[:UPDATE_HISTORY_RETENTION]


def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    finished_at = float(record.get("finished_at") or time.time())
    started_at = _optional_float(record.get("started_at"))
    elapsed = _optional_float(record.get("elapsed_seconds"))
    if elapsed is None and started_at is not None:
        elapsed = max(0.0, finished_at - started_at)
    suffix = time.time_ns() % 1_000_000
    return {
        "id": str(record.get("id") or f"update-{int(finished_at * 1000)}-{suffix}"),
        "status": "success" if record.get("status") == "success" else "failed",
        "phase": str(record.get("phase") or "idle"),
        "task_source": str(record.get("task_source") or "数据更新"),
        "fetch_window_label": str(record.get("fetch_window_label") or ""),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(elapsed, 2) if elapsed is not None else None,
        "resources_updated": _non_negative_int(record.get("resources_updated")),
        "resources_created": _non_negative_int(record.get("resources_created")),
        "predicted_resources": _non_negative_int(record.get("predicted_resources")),
        "total_new_points": _non_negative_int(record.get("total_new_points")),
        "message": str(record.get("message") or ""),
        "error": str(record.get("error")) if record.get("error") else None,
    }


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
