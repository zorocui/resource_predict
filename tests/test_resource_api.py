from __future__ import annotations

from types import SimpleNamespace

from flask import Flask

from resource_predict.api.resources import register_resource_routes
from resource_predict.services.store import action_priority, matches_query, safe_int


def _resource(
    resource_id: str,
    action: str,
    *,
    resource_type: str = "k8s_workload",
    confidence: str = "medium",
    best_methods: dict | None = None,
) -> dict:
    return {
        "resource_id": resource_id,
        "resource_type": resource_type,
        "scaling_advice": {"action": action, "confidence": confidence},
        "best_methods": best_methods or {},
        "anomaly_score": 0.0,
    }


def _app(resources: list[dict], details: dict[str, dict] | None = None) -> Flask:
    details = details or {}
    app = Flask(__name__)
    helpers = {
        "safe_int": safe_int,
        "action_priority": action_priority,
        "matches_query": matches_query,
        "get_summary": lambda: {"resources": resources},
        "get_resource_detail": lambda resource_id, **_kwargs: details.get(resource_id),
        "get_resource_charts": lambda resource_id, **_kwargs: (
            {"resource_id": resource_id, "charts": details.get(resource_id, {}).get("charts", {})}
            if resource_id in details else None
        ),
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


def test_advice_summary_counts_full_filtered_scope_not_current_page():
    resources = [
        _resource("vm-hot", "scale_out", resource_type="openstack_vm", confidence="high", best_methods={"cpu": "arima"}),
        _resource("vm-cold", "scale_in", resource_type="openstack_vm", confidence="low", best_methods={"cpu": "rolling_mean"}),
        _resource("wl-hot", "scale_out_candidate", confidence="high", best_methods={"cpu_limit": "arima", "memory_limit": "arima"}),
        _resource("wl-cold", "scale_in_candidate", confidence="high", best_methods={"cpu_request": "rolling_mean"}),
        _resource("wl-hold", "hold", confidence="medium", best_methods={"cpu_limit": "seasonal_naive"}),
    ]
    app = _app(resources)

    response = app.test_client().get("/api/resources/advice-summary?confidence=high")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 3
    assert payload["action_counts"]["scale_out"] == 1
    assert payload["action_counts"]["scale_out_candidate"] == 1
    assert payload["action_counts"]["scale_in_candidate"] == 1
    assert payload["resource_type_counts"] == {"openstack_vm": 1, "k8s_workload": 2}
    assert payload["best_method_counts"]["arima"] == 3
    assert payload["best_method_counts"]["rolling_mean"] == 1


def test_resources_endpoint_returns_observed_stats_from_summary_item():
    app = _app(
        [
            {
                **_resource("workload", "hold"),
                "observed_stats": {"memory_limit": {"avg": 2.5, "p95": 3.85, "peak": 4.0}},
            }
        ],
    )

    response = app.test_client().get("/api/resources?page_size=20")

    assert response.status_code == 200
    payload = response.get_json()
    stats = payload["items"][0]["observed_stats"]["memory_limit"]
    assert stats["avg"] == 2.5
    assert stats["peak"] == 4.0
    assert stats["p95"] == 3.85


def test_resource_detail_can_skip_charts():
    app = _app([], {"vm-1": {"resource_id": "vm-1", "charts": {"cpu": {"y_train": [1]}}}})

    response = app.test_client().get("/api/resources/vm-1?include_charts=false")

    assert response.status_code == 200


def test_resource_detail_rejects_invalid_include_charts_value():
    app = _app([], {"vm-1": {"resource_id": "vm-1"}})

    response = app.test_client().get("/api/resources/vm-1?include_charts=maybe")

    assert response.status_code == 400


def test_batch_details_rejects_invalid_include_charts_value():
    app = _app([], {"vm-1": {"resource_id": "vm-1"}})

    response = app.test_client().get("/api/resources/details?ids=vm-1&include_charts=maybe")

    assert response.status_code == 400


def test_resource_charts_endpoint_forwards_filters():
    calls = []
    app = Flask(__name__)
    helpers = {
        "safe_int": safe_int,
        "action_priority": action_priority,
        "matches_query": matches_query,
        "get_summary": lambda: {"resources": []},
        "get_resource_detail": lambda _resource_id, **_kwargs: None,
        "get_resource_charts": lambda resource_id, **kwargs: calls.append((resource_id, kwargs)) or {
            "resource_id": resource_id,
            "charts": {"cpu": {}},
        },
        "prediction_pending_for": lambda _resource_id: None,
    }
    register_resource_routes(app, helpers)

    response = app.test_client().get("/api/resources/vm-1/charts?metric=cpu&history_points=500")

    assert response.status_code == 200
    assert calls == [(
        "vm-1",
        {
            "history_points": 500,
            "metric": "cpu",
            "container": None,
            "start_ms": None,
            "end_ms": None,
        },
    )]


def test_resource_charts_rejects_invalid_history_points():
    app = _app([], {})

    response = app.test_client().get("/api/resources/vm-1/charts?history_points=0")

    assert response.status_code == 400


def test_resource_charts_omitted_history_points_stays_unbounded():
    calls = []
    app = Flask(__name__)
    helpers = {
        "settings": SimpleNamespace(generation=SimpleNamespace(api_page_size_default=20, api_page_size_max=200)),
        "safe_int": safe_int,
        "action_priority": action_priority,
        "matches_query": matches_query,
        "get_summary": lambda: {"resources": []},
        "get_resource_detail": lambda _resource_id, **_kwargs: None,
        "get_resource_charts": lambda resource_id, **kwargs: calls.append((resource_id, kwargs)) or {
            "resource_id": resource_id,
            "charts": {"cpu": {}},
        },
        "prediction_pending_for": lambda _resource_id: None,
    }
    register_resource_routes(app, helpers)

    response = app.test_client().get("/api/resources/vm-1/charts?metric=cpu")

    assert response.status_code == 200
    assert calls == [(
        "vm-1",
        {
            "history_points": None,
            "metric": "cpu",
            "container": None,
            "start_ms": None,
            "end_ms": None,
        },
    )]
