from __future__ import annotations

from flask import Flask

from resource_predict.api.resources import register_resource_routes
from resource_predict.services.store import action_priority, matches_query, safe_int


def _resource(resource_id: str, action: str) -> dict:
    return {
        "resource_id": resource_id,
        "resource_type": "k8s_workload",
        "scaling_advice": {"action": action, "confidence": "medium"},
        "anomaly_score": 0.0,
    }


def _app(resources: list[dict]) -> Flask:
    app = Flask(__name__)
    helpers = {
        "safe_int": safe_int,
        "action_priority": action_priority,
        "matches_query": matches_query,
        "get_summary": lambda: {"resources": resources},
        "get_resource_detail": lambda _resource_id: None,
        "prediction_pending_for": lambda _resource_id: None,
    }
    register_resource_routes(app, helpers)
    return app


def test_resources_endpoint_uses_server_side_pagination():
    app = _app([_resource(f"res-{i:03d}", "hold") for i in range(45)])

    response = app.test_client().get("/api/resources?page=3&page_size=20&sort_by=resource_id")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 45
    assert payload["page"] == 3
    assert payload["page_size"] == 20
    assert [item["resource_id"] for item in payload["items"]] == [f"res-{i:03d}" for i in range(40, 45)]


def test_scale_out_filter_includes_candidate_actions():
    app = _app(
        [
            _resource("ready", "scale_out"),
            _resource("candidate", "scale_out_candidate"),
            _resource("hold", "hold"),
        ]
    )

    response = app.test_client().get("/api/resources?action=scale_out&page_size=20&sort_by=resource_id")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 2
    assert [item["resource_id"] for item in payload["items"]] == ["candidate", "ready"]
