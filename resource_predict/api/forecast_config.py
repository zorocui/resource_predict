from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, request

from resource_predict.services.forecast_config import (
    ForecastConfigValidationError,
    read_forecast_config_payload,
    write_forecast_config,
)


logger = logging.getLogger(__name__)


def register_forecast_config_routes(app: Flask) -> None:
    @app.get("/api/forecast-config")
    def api_get_forecast_config():
        try:
            return jsonify(read_forecast_config_payload())
        except ForecastConfigValidationError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.put("/api/forecast-config")
    def api_save_forecast_config():
        body: Any = request.get_json(silent=True) or {}
        try:
            config = write_forecast_config(body)
        except ForecastConfigValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("[api] failed to save forecast config")
            return jsonify({"error": str(exc)}), 500
        return jsonify({"saved": True, **read_forecast_config_payload(), **config})
