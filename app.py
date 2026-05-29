from __future__ import annotations

import os

from flask import Flask

from resource_predict.api.cluster_configs import register_cluster_config_routes
from resource_predict.api.forecast_config import register_forecast_config_routes
from resource_predict.api.pages import register_page_routes
from resource_predict.api.resources import register_resource_routes
from resource_predict.api.scaling import register_scaling_routes
from resource_predict.api.updates import register_update_routes
from resource_predict.data.updater import start_background_updater
from resource_predict.logging_setup import setup_application_logging
from resource_predict.services.k8s_ingest import start_k8s_background_updater
from resource_predict.services.store import (
    ForecastStore,
    action_priority,
    matches_query,
    prediction_pending_for,
    safe_int,
)
from resource_predict.settings import settings


def create_app() -> Flask:
    setup_application_logging()
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    app = Flask(
        __name__,
        static_folder=settings.app.static_folder,
        template_folder=settings.app.template_folder,
    )

    store = ForecastStore()
    route_helpers = {
        "safe_int": safe_int,
        "action_priority": action_priority,
        "matches_query": matches_query,
        "get_summary": store.get_summary,
        "get_resource_detail": store.get_resource_detail,
        "prediction_pending_for": prediction_pending_for,
    }
    register_page_routes(app, route_helpers)
    register_resource_routes(app, route_helpers)
    register_scaling_routes(app, route_helpers)
    register_update_routes(app)
    register_cluster_config_routes(app)
    register_forecast_config_routes(app)
    start_background_updater()
    start_k8s_background_updater()

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host=settings.app.host, port=settings.app.port, debug=settings.app.debug)
