from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, request

from resource_predict.services.cluster_configs import ClusterConfigValidationError
from resource_predict.services.runtime_config import RuntimeConfigValidationError
from resource_predict.services.system_config import read_system_config_payload, save_system_config_payload


logger = logging.getLogger(__name__)


def register_system_config_routes(app: Flask) -> None:
    @app.get("/api/system-config")
    def api_get_system_config():
        try:
            return jsonify(read_system_config_payload())
        except Exception as exc:
            logger.exception("[api] failed to read system config")
            return jsonify({"error": str(exc)}), 500

    @app.put("/api/system-config")
    def api_save_system_config():
        body: Any = request.get_json(silent=True) or {}
        try:
            payload = save_system_config_payload(body)
        except RuntimeConfigValidationError as exc:
            return jsonify({"error": str(exc), "field": exc.field}), 400
        except ClusterConfigValidationError as exc:
            return jsonify({"error": str(exc), "field": "clusters"}), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("[api] failed to save system config")
            return jsonify({"error": str(exc)}), 500
        return jsonify({"saved": True, **payload})

