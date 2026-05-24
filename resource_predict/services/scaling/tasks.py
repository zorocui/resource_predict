from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from resource_predict.settings import settings
from resource_predict.services.scaling.cluster_config import get_cluster_config
from resource_predict.services.scaling.command_runner import run_ssh_command
from resource_predict.services.scaling.executor import build_openstack_resize_confirm_command, build_scaling_plan
from resource_predict.services.scaling.snapshot import apply_scaling_success_snapshot


TASKS_PATH = Path(settings.app.out_dir) / "scaling_tasks.json"
_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def create_scaling_task(
    resource: Dict[str, Any],
    *,
    mode: str = "dry_run",
    operator: str = "",
    allow_create_flavor: bool = False,
) -> Dict[str, Any]:
    mode = mode if mode in {"dry_run", "execute"} else "dry_run"
    resource_id = str(resource.get("resource_id", "")).strip()
    running = get_active_task_for_resource(resource_id)
    if running is not None:
        logger.warning(
            "[scaling] task rejected: resource_id=%s reason=active_task_exists active_task_id=%s",
            resource_id,
            running.get("task_id", "-"),
        )
        raise RuntimeError(f"resource {resource_id} already has a scaling task running")

    task_id = f"scale-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    now = _now_ms()
    task = {
        "task_id": task_id,
        "resource_id": resource_id,
        "mode": mode,
        "operator": operator,
        "allow_create_flavor": bool(allow_create_flavor),
        "status": "queued",
        "created_at_ms": now,
        "updated_at_ms": now,
        "plan": None,
        "results": [],
        "error": "",
    }
    _upsert_task(task)
    logger.info(
        "[scaling] task queued: task_id=%s resource_id=%s mode=%s operator=%s allow_create_flavor=%s",
        task_id,
        resource_id,
        mode,
        operator or "-",
        bool(allow_create_flavor),
    )
    thread = threading.Thread(
        target=_run_task,
        args=(task_id, resource),
        daemon=True,
        name=f"scaling-{task_id}",
    )
    thread.start()
    return task


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    tasks = _read_tasks()
    for task in tasks:
        if str(task.get("task_id")) == str(task_id):
            return task
    return None


def get_history(resource_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    rid = str(resource_id)
    rows = [x for x in _read_tasks() if str(x.get("resource_id")) == rid]
    rows.sort(key=lambda x: int(x.get("created_at_ms", 0)), reverse=True)
    return rows[: max(1, min(limit, 100))]


def get_active_task_for_resource(resource_id: str) -> Optional[Dict[str, Any]]:
    if not resource_id:
        return None
    for task in _read_tasks():
        if str(task.get("resource_id")) != str(resource_id):
            continue
        if str(task.get("status")) in {"queued", "running", "waiting_confirm", "confirming"}:
            return task
    return None


def confirm_scaling_task(task_id: str, *, operator: str = "") -> Dict[str, Any]:
    task = get_task(task_id)
    if task is None:
        raise RuntimeError("scaling task not found")
    if str(task.get("status")) != "waiting_confirm":
        raise RuntimeError("scaling task is not waiting for manual confirm")
    plan = task.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("scaling task has no executable plan")
    if str(plan.get("resource_type")) != "openstack_vm":
        raise RuntimeError("manual confirm only supports OpenStack resize tasks")
    resource_id = str(task.get("resource_id") or plan.get("resource_id") or "").strip()
    cluster = str(plan.get("cluster") or "").strip()
    details = plan.get("details", {}) if isinstance(plan.get("details"), dict) else {}
    instance_id = str(details.get("instance_id") or "").strip()
    if not instance_id:
        raise RuntimeError("scaling task is missing OpenStack instance_id")
    if not cluster:
        raise RuntimeError("scaling task is missing cluster")

    _patch_task(
        task_id,
        {
            "status": "confirming",
            "phase": "confirming",
            "confirm_operator": operator,
            "updated_at_ms": _now_ms(),
        },
    )
    thread = threading.Thread(
        target=_run_manual_confirm,
        args=(task_id,),
        daemon=True,
        name=f"scaling-confirm-{task_id}",
    )
    thread.start()
    logger.info(
        "[scaling] manual confirm queued: task_id=%s resource_id=%s operator=%s",
        task_id,
        resource_id or "-",
        operator or "-",
    )
    return get_task(task_id) or task


def _run_task(task_id: str, resource: Dict[str, Any]) -> None:
    task = get_task(task_id)
    if task is None:
        return
    _patch_task(task_id, {"status": "running", "phase": "loading_config", "updated_at_ms": _now_ms()})
    logger.info(
        "[scaling] task started: task_id=%s resource_id=%s mode=%s",
        task_id,
        task.get("resource_id", "-"),
        task.get("mode", "-"),
    )
    try:
        spec = resource.get("spec", {}) if isinstance(resource, dict) else {}
        cluster = str(spec.get("cluster", "")).strip() if isinstance(spec, dict) else ""
        cluster_cfg = get_cluster_config(cluster)
        task = get_task(task_id) or task
        mode = str(task.get("mode", "dry_run"))
        plan = build_scaling_plan(
            resource,
            cluster_cfg,
            allow_create_flavor=bool(task.get("allow_create_flavor")),
        )
        updates: Dict[str, Any] = {
            "plan": plan.to_dict(),
            "phase": "plan_built",
            "command_total": len(plan.commands),
            "command_index": 0,
            "current_command": "",
            "updated_at_ms": _now_ms(),
        }
        _patch_task(task_id, updates)
        logger.info(
            "[scaling] plan built: task_id=%s resource_id=%s mode=%s resource_type=%s cluster=%s action=%s commands=%s warnings=%s",
            task_id,
            plan.resource_id,
            mode,
            plan.resource_type,
            plan.cluster,
            plan.action,
            _format_commands_for_log(plan.commands),
            plan.warnings or [],
        )
        if mode == "dry_run":
            updates.update({"status": "success", "results": [], "message": "dry_run only generated commands"})
            _patch_task(task_id, updates)
            logger.info(
                "[scaling] dry_run success: task_id=%s resource_id=%s commands=%s warnings=%s",
                task_id,
                plan.resource_id,
                _format_commands_for_log(plan.commands),
                plan.warnings or [],
            )
            return

        results = []
        for idx, remote_command in enumerate(plan.commands, start=1):
            _patch_task(
                task_id,
                {
                    **updates,
                    "phase": "executing_command",
                    "command_index": idx,
                    "current_command": remote_command,
                    "results": results,
                    "updated_at_ms": _now_ms(),
                },
            )
            logger.info(
                "[scaling] execute command started: task_id=%s resource_id=%s command=%s",
                task_id,
                plan.resource_id,
                remote_command,
            )
            result = run_ssh_command(
                cluster_cfg,
                remote_command,
                timeout_seconds=int(cluster_cfg.get("command_timeout_seconds", 300)),
            )
            results.append(result)
            _patch_task(
                task_id,
                {
                    **updates,
                    "phase": "command_finished",
                    "command_index": idx,
                    "current_command": remote_command,
                    "results": results,
                    "updated_at_ms": _now_ms(),
                },
            )
            if int(result.get("exit_code", 1)) != 0:
                logger.error(
                    "[scaling] execute command failed: task_id=%s resource_id=%s exit_code=%s stderr=%s stdout=%s",
                    task_id,
                    plan.resource_id,
                    result.get("exit_code"),
                    _trim_for_log(result.get("stderr")),
                    _trim_for_log(result.get("stdout")),
                )
                _patch_task(
                    task_id,
                    {
                        **updates,
                        "status": "failed",
                        "phase": "failed",
                        "results": results,
                        "error": result.get("stderr") or "scaling command failed",
                        "updated_at_ms": _now_ms(),
                    },
                )
                return
            logger.info(
                "[scaling] execute command success: task_id=%s resource_id=%s exit_code=%s duration_seconds=%s stdout=%s",
                task_id,
                plan.resource_id,
                result.get("exit_code"),
                result.get("duration_seconds"),
                _trim_for_log(result.get("stdout")),
            )
        if (
            plan.resource_type == "openstack_vm"
            and mode == "execute"
            and not bool(cluster_cfg.get("auto_confirm_resize", False))
        ):
            _patch_task(
                task_id,
                {
                    **updates,
                    "status": "waiting_confirm",
                    "phase": "waiting_confirm",
                    "results": results,
                    "message": "resize completed; waiting for manual OpenStack resize confirm",
                    "updated_at_ms": _now_ms(),
                },
            )
            logger.info(
                "[scaling] waiting manual confirm: task_id=%s resource_id=%s commands=%d",
                task_id,
                plan.resource_id,
                len(plan.commands),
            )
            return
        local_update: Dict[str, Any] = {}
        try:
            _patch_task(
                task_id,
                {
                    **updates,
                    "phase": "updating_snapshot",
                    "results": results,
                    "updated_at_ms": _now_ms(),
                },
            )
            local_update = apply_scaling_success_snapshot(plan)
        except Exception as exc:
            local_update = {"status": "failed", "error": str(exc)}
            logger.exception(
                "[scaling] local snapshot update failed: task_id=%s resource_id=%s error=%s",
                task_id,
                plan.resource_id,
                exc,
            )
        _patch_task(
            task_id,
            {
                **updates,
                "status": "success",
                "phase": "completed",
                "results": results,
                "local_update": local_update,
                "updated_at_ms": _now_ms(),
            },
        )
        logger.info(
            "[scaling] execute success: task_id=%s resource_id=%s commands=%d warnings=%s local_update=%s",
            task_id,
            plan.resource_id,
            len(plan.commands),
            plan.warnings or [],
            local_update,
        )
    except Exception as exc:
        resource_id = resource.get("resource_id", "-") if isinstance(resource, dict) else "-"
        logger.exception(
            "[scaling] task failed: task_id=%s resource_id=%s error=%s",
            task_id,
            resource_id,
            exc,
        )
        _patch_task(
            task_id,
            {
                "status": "failed",
                "error": str(exc),
                "updated_at_ms": _now_ms(),
            },
        )


def _run_manual_confirm(task_id: str) -> None:
    task = get_task(task_id)
    if task is None:
        return
    plan_obj = task.get("plan")
    if not isinstance(plan_obj, dict):
        _patch_task(task_id, {"status": "failed", "error": "scaling task has no plan", "updated_at_ms": _now_ms()})
        return
    try:
        cluster = str(plan_obj.get("cluster") or "").strip()
        details = plan_obj.get("details", {}) if isinstance(plan_obj.get("details"), dict) else {}
        instance_id = str(details.get("instance_id") or "").strip()
        cluster_cfg = get_cluster_config(cluster)
        remote_command = build_openstack_resize_confirm_command(instance_id, cluster_cfg)
        _patch_task(
            task_id,
            {
                "phase": "executing_confirm",
                "current_command": remote_command,
                "updated_at_ms": _now_ms(),
            },
        )
        logger.info(
            "[scaling] manual confirm command started: task_id=%s resource_id=%s command=%s",
            task_id,
            plan_obj.get("resource_id", "-"),
            remote_command,
        )
        result = run_ssh_command(
            cluster_cfg,
            remote_command,
            timeout_seconds=int(cluster_cfg.get("command_timeout_seconds", 300)),
        )
        results = list(task.get("results", [])) if isinstance(task.get("results"), list) else []
        results.append(result)
        _patch_task(
            task_id,
            {
                "phase": "confirm_command_finished",
                "results": results,
                "updated_at_ms": _now_ms(),
            },
        )
        if int(result.get("exit_code", 1)) != 0:
            _patch_task(
                task_id,
                {
                    "status": "waiting_confirm",
                    "phase": "waiting_confirm",
                    "results": results,
                    "error": result.get("stderr") or "resize confirm failed",
                    "updated_at_ms": _now_ms(),
                },
            )
            logger.error(
                "[scaling] manual confirm failed: task_id=%s resource_id=%s exit_code=%s stderr=%s stdout=%s",
                task_id,
                plan_obj.get("resource_id", "-"),
                result.get("exit_code"),
                _trim_for_log(result.get("stderr")),
                _trim_for_log(result.get("stdout")),
            )
            return

        local_update: Dict[str, Any] = {}
        try:
            _patch_task(
                task_id,
                {
                    "phase": "updating_snapshot",
                    "results": results,
                    "updated_at_ms": _now_ms(),
                },
            )
            from types import SimpleNamespace

            local_update = apply_scaling_success_snapshot(
                SimpleNamespace(
                    resource_id=plan_obj.get("resource_id"),
                    target_spec=plan_obj.get("target_spec", {}),
                    details=plan_obj.get("details", {}),
                )
            )
        except Exception as exc:
            local_update = {"status": "failed", "error": str(exc)}
            logger.exception(
                "[scaling] local snapshot update after manual confirm failed: task_id=%s resource_id=%s error=%s",
                task_id,
                plan_obj.get("resource_id", "-"),
                exc,
            )
        _patch_task(
            task_id,
            {
                "status": "success",
                "phase": "completed",
                "results": results,
                "local_update": local_update,
                "message": "manual resize confirm completed",
                "error": "",
                "updated_at_ms": _now_ms(),
            },
        )
        logger.info(
            "[scaling] manual confirm success: task_id=%s resource_id=%s local_update=%s",
            task_id,
            plan_obj.get("resource_id", "-"),
            local_update,
        )
    except Exception as exc:
        logger.exception("[scaling] manual confirm task failed: task_id=%s error=%s", task_id, exc)
        _patch_task(
            task_id,
            {
                "status": "waiting_confirm",
                "error": str(exc),
                "updated_at_ms": _now_ms(),
            },
        )


def _read_tasks() -> List[Dict[str, Any]]:
    with _LOCK:
        if not TASKS_PATH.exists():
            return []
        try:
            data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []


def _write_tasks(tasks: List[Dict[str, Any]]) -> None:
    with _LOCK:
        TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = TASKS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tasks[-1000:], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(TASKS_PATH)


def _upsert_task(task: Dict[str, Any]) -> None:
    tasks = _read_tasks()
    tasks = [x for x in tasks if str(x.get("task_id")) != str(task.get("task_id"))]
    tasks.append(task)
    _write_tasks(tasks)


def _patch_task(task_id: str, patch: Dict[str, Any]) -> None:
    tasks = _read_tasks()
    for task in tasks:
        if str(task.get("task_id")) == str(task_id):
            task.update(patch)
            break
    _write_tasks(tasks)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _format_commands_for_log(commands: List[str]) -> List[str]:
    return [_trim_for_log(command, limit=1200) for command in commands]


def _trim_for_log(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"

