from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Type


def run_update_task_sync(
    target_func: Callable[..., Dict[str, Any]],
    *args: Any,
    busy_error_cls: Type[BaseException],
    logger: logging.Logger,
    timeout_seconds: int = 600,
    extra_timeout_seconds: int = 60,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run an update function in a worker thread and wait for a bounded result."""
    holder: Dict[str, Any] = {}

    def _run() -> None:
        try:
            holder["result"] = target_func(*args, **kwargs)
        except busy_error_cls as exc:
            holder["busy"] = str(exc)
        except Exception as exc:
            logger.exception("[api] update thread failed")
            holder["fatal"] = str(exc)

    t = threading.Thread(target=_run, daemon=True, name="api-update-sync")
    t.start()
    t.join(timeout=timeout_seconds)

    if "result" not in holder and "busy" not in holder and "fatal" not in holder:
        waited = 0
        while waited < extra_timeout_seconds:
            t.join(timeout=2)
            waited += 2
            if "result" in holder or "busy" in holder or "fatal" in holder:
                break

    return holder


def start_update_task_async(
    target_func: Callable[..., Dict[str, Any]],
    *args: Any,
    busy_error_cls: Type[BaseException],
    logger: logging.Logger,
    thread_name: str = "api-update-data",
    **kwargs: Any,
) -> None:
    """Start an update task and return immediately; status is exposed by API."""

    def _run() -> None:
        started = time.time()
        logger.info("[api] async update task started: %s", thread_name)
        try:
            result = target_func(*args, **kwargs)
            logger.info(
                "[api] async update task finished: %s, elapsed=%.2fs, success=%s",
                thread_name,
                time.time() - started,
                result.get("success") if isinstance(result, dict) else None,
            )
        except busy_error_cls as exc:
            logger.warning("[api] update task rejected because another task is running: %s", exc)
        except Exception:
            logger.exception("[api] async update task failed")

    threading.Thread(target=_run, daemon=True, name=thread_name).start()
