from __future__ import annotations

from typing import Any, Callable, Dict, List

from flask import Flask, jsonify, request

from resource_predict.settings import settings
from resource_predict.services.urgency import compute_urgency_score


def register_resource_routes(app: Flask, helpers: Dict[str, Callable[..., Any]]) -> None:
    get_summary = helpers["get_summary"]
    matches_query = helpers["matches_query"]
    safe_int = helpers["safe_int"]
    action_priority = helpers["action_priority"]
    prediction_pending_for = helpers["prediction_pending_for"]
    get_resource_detail = helpers["get_resource_detail"]

    @app.get("/api/resources")
    def api_resources():
        summary = get_summary()
        resources = summary.get("resources", []) if isinstance(summary, dict) else []
        q = (request.args.get("q") or "").strip().lower()
        action_filter = (request.args.get("action") or "").strip().lower()
        sort_by = (request.args.get("sort_by") or "urgency_score").strip().lower()
        top_n = safe_int(request.args.get("top_n"), 0)
        page = max(1, safe_int(request.args.get("page"), 1))
        page_size = safe_int(request.args.get("page_size"), settings.generation.api_page_size_default)
        page_size = max(1, min(page_size, settings.generation.api_page_size_max))

        rows = [x for x in resources if isinstance(x, dict)]
        if q:
            rows = [x for x in rows if matches_query(x, q)]
        if action_filter in {"scale_out", "scale_in", "hold", "mixed"}:
            if action_filter == "mixed":
                rows = [
                    x for x in rows
                    if bool((x.get("scaling_advice", {}) or {}).get("has_mixed_signals"))
                ]
            else:
                rows = [
                    x for x in rows
                    if str((x.get("scaling_advice", {}) or {}).get("action", "hold")).lower() == action_filter
                    and not bool((x.get("scaling_advice", {}) or {}).get("has_mixed_signals"))
                ]
        rows = [
            {**x, "urgency_score": compute_urgency_score(x, settings.decision)}
            for x in rows
        ]

        if sort_by == "resource_id":
            rows.sort(key=lambda x: str(x.get("resource_id", "")))
        elif sort_by == "anomaly_score":
            rows.sort(key=lambda x: -float(x.get("anomaly_score", 0.0)))
        else:
            rows.sort(
                key=lambda x: (
                    -float(x.get("urgency_score", 0.0)),
                    -action_priority(x),
                    -float(x.get("anomaly_score", 0.0)),
                    str(x.get("resource_id", "")),
                )
            )

        total = len(rows)
        if top_n > 0:
            data = rows[:top_n]
            return jsonify({"total": total, "page": 1, "page_size": top_n, "items": data})

        start = (page - 1) * page_size
        end = start + page_size
        items = rows[start:end]
        return jsonify({"total": total, "page": page, "page_size": page_size, "items": items})

    @app.get("/api/resources/advice-summary")
    def api_advice_summary():
        summary = get_summary()
        resources = summary.get("resources", []) if isinstance(summary, dict) else []
        q = (request.args.get("q") or "").strip().lower()
        action_filter = (request.args.get("action") or "").strip().lower()
        rows = [x for x in resources if isinstance(x, dict)]
        if q:
            rows = [x for x in rows if matches_query(x, q)]
        if action_filter in {"scale_out", "scale_in", "hold"}:
            rows = [
                x
                for x in rows
                if str((x.get("scaling_advice", {}) or {}).get("action", "hold")).lower() == action_filter
            ]

        counts = {"scale_out": 0, "scale_in": 0, "hold": 0, "mixed": 0}
        confidence_counts = {"high": 0, "medium": 0, "low": 0}
        for item in rows:
            advice = item.get("scaling_advice", {})
            confidence = str(advice.get("confidence", "medium"))
            if confidence not in confidence_counts:
                confidence = "medium"
            if bool(advice.get("has_mixed_signals")):
                counts["mixed"] += 1
            else:
                action = str(advice.get("action", "hold"))
                if action not in counts:
                    action = "hold"
                counts[action] += 1
            confidence_counts[confidence] += 1

        return jsonify(
            {
                "total": len(rows),
                "action_counts": counts,
                "confidence_counts": confidence_counts,
            }
        )

    @app.get("/api/resources/<resource_id>")
    def api_resource_detail(resource_id: str):
        pending_status = prediction_pending_for(resource_id)
        if pending_status is not None:
            return jsonify(
                {
                    "resource": {
                        "resource_id": resource_id,
                        "prediction_pending": True,
                    },
                    "status": pending_status,
                }
            ), 202
        detail = get_resource_detail(resource_id)
        if detail is None:
            return jsonify({"error": "resource not found"}), 404
        return jsonify({"resource": detail})

    @app.get("/api/resources/details")
    def api_resource_details():
        raw_ids = request.args.get("ids") or ""
        resource_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
        if not resource_ids:
            return jsonify({"resources": []})

        items: List[Dict[str, Any]] = []
        for resource_id in resource_ids[:100]:
            pending_status = prediction_pending_for(resource_id)
            if pending_status is not None:
                items.append(
                    {
                        "resource_id": resource_id,
                        "prediction_pending": True,
                    }
                )
                continue
            detail = get_resource_detail(resource_id)
            if detail is not None:
                items.append(detail)
        return jsonify({"resources": items})
