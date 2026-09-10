"""Read-only outcome reports and reproducible evidence exports."""
from __future__ import annotations

import csv
import io
from resource_predict.sqlite_runtime import sqlite3

from flask import Flask, Response, jsonify, request

from resource_predict.pipeline.output_paths import all_scoped_out_dirs
from resource_predict.services.scaling.effects import event_evidence, list_events, outcome_report


def register_scaling_effect_routes(app: Flask, out_dirs_provider=None) -> None:
    directories = out_dirs_provider or (lambda: [path for _, path in all_scoped_out_dirs()])

    def filters():
        values = {key: (request.args.get(key) or "").strip() for key in ("resource_type", "action", "status", "q")}
        allowed = {
            "resource_type": {"", "openstack_vm", "k8s_workload"},
            "action": {"", "scale_in", "scale_out", "mixed", "unknown"},
            "status": {"", "executing", "awaiting_effective", "observing", "insufficient_data", "evaluated", "failed",
                       "interrupted", "basis_changed", "missing_baseline", "capture_failed", "evidence_conflict", "expired"},
        }
        for key, options in allowed.items():
            if values[key] not in options:
                raise ValueError(f"invalid {key}")
        if len(values["q"]) > 256:
            raise ValueError("query too long")
        for key in ("from_ms", "to_ms"):
            raw = request.args.get(key)
            if raw:
                values[key] = int(raw)
                if not 0 <= values[key] <= 2**63-1:
                    raise ValueError("report timestamps must be nonnegative")
        if "from_ms" in values and "to_ms" in values and values["from_ms"] >= values["to_ms"]:
            raise ValueError("from_ms must be earlier than to_ms")
        return values

    @app.get("/api/scaling-effects")
    def api_scaling_effects():
        try:
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))
            if not 1 <= page <= 1000000 or not 1 <= page_size <= 200:
                raise ValueError("page must be positive; page_size must be 1..200")
            return jsonify(outcome_report(directories(), page=page, page_size=page_size, **filters()))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except sqlite3.Error:
            return jsonify(error="成效账本读取失败，请检查服务日志与存储"), 503

    @app.get("/api/scaling-effects/<task_id>")
    @app.get("/api/scaling-effects/<task_id>/evidence.json")
    def api_scaling_effect_detail(task_id: str):
        try:
            payload = event_evidence(directories(), task_id)
        except sqlite3.Error:
            return jsonify(error="成效证据读取失败"), 503
        if payload is None:
            return jsonify(error="outcome event not found"), 404
        response = jsonify(payload)
        if request.path.endswith("/evidence.json"):
            response.headers["Content-Disposition"] = 'attachment; filename="scaling-evidence.json"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/scaling-effects/export.csv")
    def api_scaling_effect_csv():
        try:
            events = list_events(directories(), **filters())
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except sqlite3.Error:
            return jsonify(error="成效账本读取失败"), 503
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow([
            "task_id", "resource_id", "resource_type", "action", "status", "reason", "policy_version",
            "started_at_ms", "effective_at_ms", "metric", "basis", "unit", "metric_status",
            "before_start_ms", "before_end_ms", "after_start_ms", "after_end_ms",
            "before_coverage", "after_coverage", "before_utilization_pct", "after_utilization_pct",
            "delta_pp", "relative_change_pct", "before_mean_usage", "after_mean_usage",
            "before_mean_capacity", "after_mean_capacity", "reclaimed_capacity", "reclaimed_unit_hours",
        ])
        for event in events:
            for metric in event.get("metrics") or [{}]:
                before, after = metric.get("before", {}), metric.get("after", {})
                row = [event.get(key) for key in ("task_id", "resource_id", "resource_type", "action", "status", "reason")]
                row += [event["policy"]["version"], event["started_at_ms"], event.get("effective_at_ms")]
                row += [metric.get(key) for key in ("metric", "basis", "unit", "status")]
                row += [before.get("start_ms"), before.get("end_ms"), after.get("start_ms"), after.get("end_ms")]
                row += [before.get("coverage"), after.get("coverage"), before.get("utilization_pct"), after.get("utilization_pct")]
                row += [metric.get("delta_pp"), metric.get("relative_change_pct"), before.get("mean_usage"), after.get("mean_usage"),
                        before.get("mean_capacity"), after.get("mean_capacity"), metric.get("reclaimed_capacity"), metric.get("reclaimed_unit_hours")]
                writer.writerow([_csv_cell(value) for value in row])
        return Response("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="scaling-effects.csv"', "Cache-Control": "no-store"})


def _csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value
