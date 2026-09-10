"""Prediction accuracy report endpoints; snapshots are explicit local writes."""
import re
import logging
from resource_predict.sqlite_runtime import sqlite3
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

from resource_predict.pipeline.output_paths import all_scoped_out_dirs
from resource_predict.services.accuracy_exports import (
    LEGACY_FIELDS, POINT_FIELDS, SUMMARY_FIELDS, csv_chunks, freeze_report, report_session,
)
from resource_predict.settings import settings

logger = logging.getLogger(__name__)


def _database_failure(exc, message):
    logger.exception("[forecast_accuracy] SQLite %s source=%s: %s", sqlite3.sqlite_version,
                     request.args.get("source", "realized"), type(exc).__name__)
    return jsonify(error=f"{message}（SQLite {sqlite3.sqlite_version}）"), 503


def _filters(values):
    result = {key: str(values.get(key) or "").strip() for key in (
        "source", "resource_type", "level", "metric", "model", "horizon", "q")}
    result["source"] = result["source"] or "realized"
    result["level"] = result["level"] or "resource"
    allowed = {"source": {"realized", "holdout", "legacy"}, "resource_type": {"", "openstack_vm", "k8s_workload"},
               "level": {"resource", "container"}, "horizon": {"", "0-1h", "1-6h", "6-24h", ">24h", "unknown"}}
    for key, options in allowed.items():
        if result[key] not in options:
            raise ValueError(f"invalid {key}")
    if any(len(value) > 256 for value in result.values()):
        raise ValueError("filter too long")
    for key in ("from_ms", "to_ms"):
        raw = values.get(key)
        if raw is not None and raw != "":
            if isinstance(raw, bool):
                raise ValueError("invalid target timestamp")
            result[key] = int(str(raw))
            if not 0 <= result[key] <= 2**63-1:
                raise ValueError("invalid target timestamp")
    if "from_ms" in result and "to_ms" in result and result["from_ms"] >= result["to_ms"]:
        raise ValueError("from_ms must be earlier than to_ms")
    return result


def register_forecast_accuracy_routes(app: Flask, out_dirs_provider=None, snapshot_dir=None):
    directories = out_dirs_provider or (lambda: [path for _, path in all_scoped_out_dirs()])

    def snapshots():
        return Path(snapshot_dir) if snapshot_dir is not None else Path(settings.app.out_dir) / "accuracy_snapshots"

    @app.get("/api/forecast-accuracy/summary")
    def api_accuracy_summary():
        from resource_predict.services.accuracy_summary import read_accuracy_summary
        try:
            return jsonify(read_accuracy_summary(directories()))
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception("[forecast_accuracy] summary read failed")
            return jsonify(error="准确率汇总读取失败，请检查服务器日志"), 503

    @app.get("/api/forecast-accuracy")
    def api_forecast_accuracy():
        try:
            filters = _filters(request.args)
            page, size = int(request.args.get("page", 1)), int(request.args.get("page_size", 50))
            if not 1 <= page <= 1000000 or not 1 <= size <= 200:
                raise ValueError("page must be 1..1000000; page_size must be 1..200")
            with report_session(directories(), **filters) as session:
                report = session.report(page=page, page_size=size)
                report["filters"] = filters
                return jsonify(report)
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
        except (sqlite3.Error, OSError) as exc:
            return _database_failure(exc, "准确性证据读取失败，不能作为无数据处理；具体原因已记录服务器日志")

    @app.get("/api/forecast-accuracy/export.csv")
    def api_forecast_accuracy_csv():
        try:
            filters = _filters(request.args)
            kind = request.args.get("kind", "summary")
            if kind not in {"summary", "points"}:
                raise ValueError("kind must be summary or points")
            if filters["source"] == "legacy" and kind == "points":
                raise ValueError("旧误差报告没有逐点证据")
            # Validate before opening the streamed response; keep the stream's own consistent transaction.
            with report_session(directories(), **filters):
                pass
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
        except (sqlite3.Error, OSError) as exc:
            return _database_failure(exc, "准确性证据读取失败；具体原因已记录服务器日志")

        paths = directories()
        def stream():
            with report_session(paths, **filters) as session:
                legacy = filters["source"] == "legacy"
                fields = LEGACY_FIELDS if legacy else POINT_FIELDS if kind == "points" else SUMMARY_FIELDS
                rows = session.points() if legacy or kind == "points" else session.report()["summary"]
                yield from csv_chunks(fields, rows)
        return Response(stream(), content_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="forecast-accuracy-{kind}.csv"', "Cache-Control": "no-store"})

    @app.post("/api/forecast-accuracy/snapshots")
    def api_accuracy_snapshot():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="snapshot filters must be a JSON object"), 400
        try:
            return jsonify(freeze_report(directories(), snapshots(), **_filters(body))), 201
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
        except (OSError, sqlite3.Error) as exc:
            return _database_failure(exc, "快照保存失败，未发布不完整的报告包；具体原因已记录服务器日志")

    @app.get("/api/forecast-accuracy/snapshots/<snapshot_id>/download")
    def api_accuracy_snapshot_download(snapshot_id):
        if not re.fullmatch(r"[a-f0-9]{32}", snapshot_id):
            return jsonify(error="snapshot not found"), 404
        path = snapshots() / f"{snapshot_id}.zip"
        if not path.is_file() or path.is_symlink():
            return jsonify(error="snapshot not found"), 404
        return send_file(path.resolve(), as_attachment=True, download_name=f"forecast-accuracy-{snapshot_id}.zip",
                         mimetype="application/zip", conditional=True)
