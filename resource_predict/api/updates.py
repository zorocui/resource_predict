from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, request

from resource_predict.data.updater import (
    UpdateBusyError,
    get_update_status,
    run_scoped_update_with_data,
    run_scoped_upsert_with_data,
    run_update,
)
from resource_predict.services.update_tasks import run_update_task_sync, start_update_task_async


logger = logging.getLogger(__name__)


def _update_busy_payload(status: dict[str, Any]) -> dict[str, Any]:
    phase = status.get("phase") or "unknown"
    message = status.get("message") or status.get("last_error") or ""
    details = f"当前阶段：{phase}"
    if message:
        details += f"；状态消息：{message}"
    return {
        "error": f"已有更新任务正在运行，请稍后再试。{details}",
        "status": status,
    }


def register_update_routes(app: Flask) -> None:
    @app.get("/api/update-status")
    def api_update_status():
        return jsonify(get_update_status())

    @app.post("/api/update-trigger")
    def api_update_trigger():
        holder = run_update_task_sync(
            run_update,
            fail_if_busy=True,
            busy_error_cls=UpdateBusyError,
            logger=logger,
        )

        if "busy" in holder:
            status = get_update_status()
            return (
                jsonify(_update_busy_payload(status)),
                409,
            )
        if "fatal" in holder:
            return (
                jsonify(
                    {
                        "error": holder["fatal"],
                        "status": get_update_status(),
                    }
                ),
                500,
            )
        if "result" not in holder:
            return (
                jsonify(
                    {
                        "error": "update task did not finish within the timeout",
                        "status": get_update_status(),
                    }
                ),
                504,
            )
        return jsonify(holder["result"])

    @app.post("/api/update-data")
    def api_update_data():
        body = request.get_json(silent=True)
        error = _validate_resource_array_body(body)
        if error:
            return jsonify({"error": error}), 400

        current_status = get_update_status()
        if current_status.get("running"):
            return (
                jsonify(_update_busy_payload(current_status)),
                409,
            )

        submitted_resource_ids = _submitted_resource_ids(body)
        logger.info(
            "[api] accepted push update data: resources=%d, ids=%s",
            len(submitted_resource_ids),
            submitted_resource_ids[:20],
        )
        start_update_task_async(
            run_scoped_update_with_data,
            body,
            fail_if_busy=True,
            busy_error_cls=UpdateBusyError,
            logger=logger,
        )
        return (
            jsonify(
                {
                    "accepted": True,
                    "message": "update data accepted; merge and prediction task started",
                    "submitted_resources": len(submitted_resource_ids),
                    "submitted_resource_ids": submitted_resource_ids,
                    "status_url": "/api/update-status",
                    "status": get_update_status(),
                }
            ),
            202,
        )

    @app.post("/api/upsert-data")
    def api_upsert_data():
        body = request.get_json(silent=True)
        error = _validate_resource_array_body(body)
        if error:
            return jsonify({"error": error}), 400

        current_status = get_update_status()
        if current_status.get("running"):
            return (
                jsonify(_update_busy_payload(current_status)),
                409,
            )

        submitted_resource_ids = _submitted_resource_ids(body)
        logger.info(
            "[api] accepted push upsert data: resources=%d, ids=%s",
            len(submitted_resource_ids),
            submitted_resource_ids[:20],
        )
        start_update_task_async(
            run_scoped_upsert_with_data,
            body,
            fail_if_busy=True,
            busy_error_cls=UpdateBusyError,
            logger=logger,
            thread_name="api-upsert-data",
        )
        return (
            jsonify(
                {
                    "accepted": True,
                    "message": "upsert data accepted; merge and prediction task started",
                    "submitted_resources": len(submitted_resource_ids),
                    "submitted_resource_ids": submitted_resource_ids,
                    "status_url": "/api/update-status",
                    "status": get_update_status(),
                }
            ),
            202,
        )


def _validate_resource_array_body(body: Any) -> str:
    if not isinstance(body, list) or not body:
        return "request body must be a non-empty JSON array"
    for idx, item in enumerate(body):
        if not isinstance(item, dict):
            return f"item {idx} must be a dict"
        if "resource_id" not in item:
            return f"item {idx} is missing resource_id"
    return ""


def _submitted_resource_ids(body: Any) -> list[str]:
    return [
        str(item.get("resource_id"))
        for item in body
        if isinstance(item, dict) and item.get("resource_id") is not None
    ]
