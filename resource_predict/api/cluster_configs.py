from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, request

from resource_predict.providers.k8s_prometheus import diagnose_k8s_prometheus
from resource_predict.data.updater import UpdateBusyError, get_update_status
from resource_predict.services.cluster_configs import (
    ClusterConfigValidationError,
    read_cluster_config_payload,
    write_k8s_prometheus_clusters,
    write_vm_scaling_clusters,
)
from resource_predict.services.k8s_ingest import run_k8s_prometheus_upsert
from resource_predict.services.update_tasks import start_update_task_async


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


def register_cluster_config_routes(app: Flask) -> None:
    @app.get("/api/cluster-configs")
    def api_get_cluster_configs():
        try:
            return jsonify(read_cluster_config_payload())
        except ClusterConfigValidationError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.put("/api/cluster-configs")
    def api_save_cluster_configs():
        body: Any = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        try:
            vm_clusters = write_vm_scaling_clusters(body.get("vm_scaling_clusters", {}))
            k8s_clusters = write_k8s_prometheus_clusters(body.get("k8s_prometheus_clusters", []))
        except ClusterConfigValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("[api] failed to save cluster configs")
            return jsonify({"error": str(exc)}), 500
        return jsonify(
            {
                "saved": True,
                "vm_scaling_clusters": vm_clusters,
                "k8s_prometheus_clusters": k8s_clusters,
            }
        )

    @app.post("/api/cluster-configs/k8s-diagnose")
    def api_diagnose_k8s_configs():
        body: Any = request.get_json(silent=True) or {}
        clusters = body.get("clusters") if isinstance(body, dict) else None
        try:
            report = diagnose_k8s_prometheus(clusters=clusters if isinstance(clusters, list) else None)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(report)

    @app.post("/api/cluster-configs/k8s-fetch")
    def api_fetch_k8s_prometheus_data():
        body: Any = request.get_json(silent=True) or {}
        clusters = body.get("clusters") if isinstance(body, dict) else None
        cluster_names = clusters if isinstance(clusters, list) else None
        full_refresh = bool(body.get("full_refresh")) if isinstance(body, dict) else False
        current_status = get_update_status()
        if current_status.get("running"):
            return (
                jsonify(_update_busy_payload(current_status)),
                409,
            )
        start_update_task_async(
            run_k8s_prometheus_upsert,
            clusters=cluster_names,
            fail_if_busy=True,
            full_refresh=full_refresh,
            busy_error_cls=UpdateBusyError,
            logger=logger,
            thread_name="api-k8s-prometheus-fetch",
        )
        return (
            jsonify(
                {
                    "accepted": True,
                    "message": "K8S Prometheus fetch accepted; merge and prediction task started",
                    "clusters": cluster_names or [],
                    "full_refresh": full_refresh,
                    "status_url": "/api/update-status",
                    "status": get_update_status(),
                }
            ),
            202,
        )
