from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from flask import Flask, jsonify, request

from resource_predict.services.scaling.tasks import confirm_scaling_task, create_scaling_task, get_history, get_task


logger = logging.getLogger(__name__)


def register_scaling_routes(app: Flask, helpers: Dict[str, Callable[..., Any]]) -> None:
    get_resource_detail = helpers["get_resource_detail"]
    safe_int = helpers["safe_int"]

    @app.post("/api/resources/<resource_id>/scale")
    def api_resource_scale(resource_id: str):
        detail = get_resource_detail(resource_id, include_charts=False)
        if detail is None:
            return jsonify({"error": "resource not found"}), 404

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        mode = str(body.get("mode") or "dry_run").strip().lower()
        if mode not in {"dry_run", "execute"}:
            return jsonify({"error": "mode only supports dry_run or execute"}), 400
        if mode == "execute" and body.get("confirm") is not True:
            return jsonify({"error": "execute mode requires confirm=true"}), 400

        try:
            task = create_scaling_task(
                detail,
                mode=mode,
                operator=str(body.get("operator") or ""),
                allow_create_flavor=bool(body.get("confirm_create_flavor")),
                target_spec_override=body.get("target_spec"),
                target_source=str(body.get("target_source") or ""),
                ignore_cooldown=bool(body.get("ignore_cooldown")),
            )
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("[api] failed to create scaling task")
            return jsonify({"error": str(exc)}), 500
        return jsonify({"accepted": True, "task": task, "task_id": task.get("task_id")}), 202

    @app.get("/api/scaling-tasks/<task_id>")
    def api_scaling_task(task_id: str):
        task = get_task(task_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify({"task": task})

    @app.post("/api/scaling-tasks/<task_id>/confirm")
    def api_scaling_task_confirm(task_id: str):
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        if body.get("confirm") is not True:
            return jsonify({"error": "manual confirm requires confirm=true"}), 400
        try:
            task = confirm_scaling_task(task_id, operator=str(body.get("operator") or ""))
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("[api] failed to confirm scaling task")
            return jsonify({"error": str(exc)}), 500
        return jsonify({"accepted": True, "task": task, "task_id": task.get("task_id")}), 202

    @app.get("/api/resources/<resource_id>/scaling-history")
    def api_resource_scaling_history(resource_id: str):
        limit = safe_int(request.args.get("limit"), 20)
        return jsonify({"resource_id": resource_id, "tasks": get_history(resource_id, limit=limit)})
