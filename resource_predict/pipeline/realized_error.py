"""Score immutable forecasts against explicitly identified, unfilled observations.

Run with ``python -m resource_predict.pipeline.realized_error --out-dir outputs/k8s``.
SQLite holds point-level evidence; JSON is a compact, selected-model report.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Iterable

from resource_predict.data.io import atomic_write_json
from resource_predict.pipeline.forecast_archive import _ARCHIVE_NAME
from resource_predict.resource_types import resource_type_of
from resource_predict.pipeline.shadow_evaluation import SCHEMA as SHADOW_SCHEMA, import_shadow, shadow_report
from resource_predict.pipeline.activation_assessment import activation_assessment

logger = logging.getLogger(__name__)
DB_NAME = "forecast_realized.sqlite3"
REPORT_NAME = "forecast_realized_report.json"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
 name TEXT PRIMARY KEY, issued_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS curves (
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE,
 resource_id TEXT NOT NULL, container TEXT NOT NULL, metric TEXT NOT NULL,
 model TEXT NOT NULL, unit TEXT NOT NULL, data_end_ms INTEGER, issued_ms INTEGER NOT NULL,
 basis TEXT NOT NULL, provenance TEXT NOT NULL, eligible INTEGER NOT NULL,
 UNIQUE(batch, resource_id, container, metric)
);
CREATE TABLE IF NOT EXISTS points (
 curve_id INTEGER NOT NULL REFERENCES curves(id) ON DELETE CASCADE,
 target_ms INTEGER NOT NULL, predicted REAL NOT NULL, actual REAL,
 scored_at_ms INTEGER, observation_source TEXT, skip_reason TEXT,
 PRIMARY KEY(curve_id, target_ms)
);
CREATE INDEX IF NOT EXISTS curves_lookup ON curves(resource_id, container, metric);
CREATE INDEX IF NOT EXISTS points_pending ON points(curve_id, target_ms) WHERE actual IS NULL;
CREATE INDEX IF NOT EXISTS curves_batch ON curves(batch);
CREATE TABLE IF NOT EXISTS calibrations (
 curve_id INTEGER PRIMARY KEY REFERENCES curves(id) ON DELETE CASCADE,
 metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upper_bounds (
 curve_id INTEGER NOT NULL, target_ms INTEGER NOT NULL, upper REAL NOT NULL,
 PRIMARY KEY(curve_id, target_ms),
 FOREIGN KEY(curve_id,target_ms) REFERENCES points(curve_id,target_ms) ON DELETE CASCADE
);
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _basis(item: dict, container: str, metric: str) -> str:
    """Conservative comparison: never mix different normalization snapshots."""
    spec = item.get("spec", {})
    kind = resource_type_of(item)
    if kind == "k8s_workload":
        if container:
            values = spec.get("containers", {}).get(container, {})
            mode = item.get("container_metric_modes", {}).get(container, {}).get(metric)
        else:
            values = {key: spec.get(key) for key in ("containers", "replicas", "replicas_observed")}
            mode = spec.get(f"{metric}_metric_mode")
        # Container series can sum several replicas; membership changes alter that basis.
        return _json([kind, values, mode, spec.get("replicas_observed"),
                      sorted(spec.get("pods_observed", []))])
    return _json([kind, {key: spec.get(key) for key in ("cpu_cores", "memory_gb", "disk_gb")}])


def _metric_blocks(item: dict, main_key: str, container_key: str):
    for metric, block in item.get(main_key, {}).items():
        yield "", metric, block
    for container, metrics in item.get(container_key, {}).items():
        for metric, block in metrics.items():
            yield container, metric, block


def _unit(item: dict, container: str, metric: str) -> str:
    kind = resource_type_of(item)
    if kind == "k8s_workload":
        mode = (item.get("container_metric_modes", {}).get(container, {}).get(metric) if container
                else item.get("spec", {}).get(f"{metric}_metric_mode"))
        return f"{kind}:{mode or 'unknown'}"
    return f"{kind}:ratio"


def _import_archives(db: sqlite3.Connection, base: Path, cutoff_ms: int) -> int:
    imported = 0
    for path in sorted((base / "forecast_history").glob("forecast_*.jsonl.gz")):
        match = _ARCHIVE_NAME.fullmatch(path.name)
        if not match or path.is_symlink() or int(match.group(1)) < cutoff_ms:
            continue
        issued = int(match.group(1))
        # One transaction per batch: corrupt/truncated input never marks it imported.
        with db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM batches WHERE name=?", (path.name,)).fetchone():
                continue
            db.execute("INSERT INTO batches VALUES (?, ?)", (path.name, issued))
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    if record.get("schema_version") != 1:
                        raise ValueError(f"unsupported forecast archive schema: {path.name}")
                    if f"forecast_{record['run_id']}.jsonl.gz" != path.name:
                        raise ValueError("archive run_id does not match filename")
                    for container, metric, curve in _metric_blocks(record, "forecasts", "container_forecasts"):
                        provenance = curve.get("provenance", {})
                        data_end = provenance.get("data_end_ms")
                        generated = provenance.get("generated_at_epoch_ms")
                        eligible = isinstance(data_end, int) and isinstance(generated, int)
                        publication = max(issued, generated) if isinstance(generated, int) else issued
                        cursor = db.execute(
                            "INSERT INTO curves(batch,resource_id,container,metric,model,unit,data_end_ms,"
                            "issued_ms,basis,provenance,eligible) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (path.name, record["resource_id"], container, metric, curve["selected_model"],
                             _unit(record, container, metric), data_end, publication,
                             _basis(record, container, metric), _json(provenance), eligible),
                        )
                        timestamps, values = curve["x_pred_ms"], curve["yhat"]
                        if not timestamps or len(timestamps) != len(values):
                            raise ValueError("unaligned archive curve")
                        for target, value in zip(timestamps, values):
                            if not isinstance(target, int) or not math.isfinite(float(value)):
                                raise ValueError("invalid archive point")
                            reason = None
                            if not eligible:
                                reason = "missing_provenance"
                            elif target <= max(publication, data_end):
                                reason = "not_future_at_publication"
                            db.execute("INSERT INTO points(curve_id,target_ms,predicted,skip_reason) VALUES (?,?,?,?)",
                                       (cursor.lastrowid, target, float(value), reason))
                        calibration = curve.get("calibration")
                        if isinstance(calibration, dict):
                            db.execute("INSERT INTO calibrations VALUES (?,?)",
                                       (cursor.lastrowid, _json(calibration)))
                            bounds = calibration.get("upper", [])
                            if bounds and len(bounds) != len(timestamps):
                                raise ValueError("unaligned calibration bounds")
                            for target, predicted, upper in zip(timestamps, values, bounds):
                                if upper is None:
                                    continue
                                if not math.isfinite(float(upper)) or float(upper) < float(predicted):
                                    raise ValueError("invalid calibration upper bound")
                                db.execute("INSERT INTO upper_bounds VALUES (?,?,?)",
                                           (cursor.lastrowid, target, float(upper)))
                    import_shadow(db, path.name, record)
        imported += 1
    return imported


def _score_evidence(db: sqlite3.Connection, items: Iterable[dict], now_ms: int) -> dict:
    scored = without_evidence = 0
    for item in items:
        evidence = item.get("observation_evidence")
        if not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or not evidence.get("source"):
            without_evidence += 1
            continue
        rid = str(item["resource_id"])
        # Evidence carries its own spec, independent of subsequent raw/spec updates.
        for container, metric, block in _metric_blocks(evidence, "metrics", "container_metrics"):
            timestamps, values = block["timestamps"], block["values"]
            if len(timestamps) != len(values):
                raise ValueError("unaligned observation evidence")
            basis = _basis(evidence, container, metric)
            observations = {}
            for target, value in zip(timestamps, values):
                if not isinstance(target, int):
                    raise ValueError("observation timestamps must be integer epoch milliseconds")
                if target <= now_ms:
                    observations[target] = float(value)
            # One indexed lookup per metric, not one DB query per historical sample.
            rows = db.execute(
                "SELECT p.curve_id,p.target_ms,c.basis FROM curves c JOIN points p ON p.curve_id=c.id "
                "WHERE c.resource_id=? AND c.container=? AND c.metric=? "
                "AND p.actual IS NULL AND c.eligible=1 AND p.target_ms>c.issued_ms "
                "AND p.target_ms>c.data_end_ms AND p.target_ms<=?",
                (rid, container, metric, now_ms),
            ).fetchall()
            for curve_id, target, expected_basis in rows:
                if target in observations:
                    actual = observations[target]
                    reason = "nonfinite_observation" if not math.isfinite(actual) else (
                        "basis_mismatch" if basis != expected_basis else None
                    )
                    if reason:
                        db.execute("UPDATE points SET skip_reason=? WHERE curve_id=? AND target_ms=?",
                                   (reason, curve_id, target))
                    else:
                        db.execute(
                            "UPDATE points SET actual=?,scored_at_ms=?,observation_source=?,skip_reason=NULL "
                            "WHERE curve_id=? AND target_ms=? AND actual IS NULL",
                            (actual, now_ms, str(evidence["source"]), curve_id, target),
                        )
                        scored += 1
    return {"newly_scored": scored, "resources_without_evidence": without_evidence}


def _report(db: sqlite3.Connection, now_ms: int, retention_days: int) -> dict:
    coverage = dict(db.execute(
        "SELECT CASE WHEN actual IS NOT NULL THEN 'scored' WHEN skip_reason IS NOT NULL THEN skip_reason "
        "WHEN target_ms>? THEN 'awaiting_target' ELSE 'awaiting_observation' END, COUNT(*) "
        "FROM points GROUP BY 1", (now_ms,),
    ))
    rows = []
    # Reduce to curve/horizon totals before sorting by wide model/unit strings.
    for model, metric, level, horizon, unit, count, mae, mse, under, magnitude in db.execute(
        "WITH totals AS (SELECT p.curve_id,CASE WHEN p.target_ms-c.data_end_ms<=3600000 THEN '0-1h' "
        "WHEN p.target_ms-c.data_end_ms<=21600000 THEN '1-6h' "
        "WHEN p.target_ms-c.data_end_ms<=86400000 THEN '6-24h' ELSE '>24h' END AS horizon, "
        "COUNT(*) AS n,SUM(ABS(p.actual-p.predicted)) AS ae,"
        "SUM((p.actual-p.predicted)*(p.actual-p.predicted)) AS se,"
        "SUM(CASE WHEN p.actual>p.predicted THEN 1.0 ELSE 0.0 END) AS under,"
        "SUM(MAX(p.actual-p.predicted,0)) AS magnitude "
        "FROM points p JOIN curves c ON c.id=p.curve_id WHERE p.actual IS NOT NULL GROUP BY 1,2) "
        "SELECT c.model,c.metric,CASE WHEN c.container='' THEN 'resource' ELSE 'container' END, "
        "t.horizon,c.unit,SUM(t.n),SUM(t.ae)/SUM(t.n),SUM(t.se)/SUM(t.n),"
        "SUM(t.under)/SUM(t.n),SUM(t.magnitude)/SUM(t.n) "
        "FROM totals t JOIN curves c ON c.id=t.curve_id GROUP BY 1,2,3,4,5 "
        "ORDER BY 1,2,3,4,5"
    ):
        rows.append(dict(model=model, metric=metric, level=level, horizon=horizon, unit=unit, count=count,
                         mae=mae, rmse=math.sqrt(mse), underestimate_rate=under,
                         mean_underestimate=magnitude))
    calibrated_rows = []
    for model, metric, level, unit, horizon, count, covered, excess, margin in db.execute(
        "SELECT c.model,c.metric,CASE WHEN c.container='' THEN 'resource' ELSE 'container' END,c.unit,"
        "CASE WHEN p.target_ms-c.data_end_ms<=3600000 THEN '0-1h' "
        "WHEN p.target_ms-c.data_end_ms<=21600000 THEN '1-6h' "
        "WHEN p.target_ms-c.data_end_ms<=86400000 THEN '6-24h' ELSE '>24h' END,COUNT(*),"
        "AVG(CASE WHEN p.actual<=u.upper THEN 1.0 ELSE 0.0 END),"
        "AVG(MAX(p.actual-u.upper,0)),AVG(u.upper-p.predicted) "
        "FROM upper_bounds u JOIN points p ON p.curve_id=u.curve_id AND p.target_ms=u.target_ms "
        "JOIN curves c ON c.id=p.curve_id WHERE p.actual IS NOT NULL GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5"
    ):
        calibrated_rows.append(dict(model=model, metric=metric, level=level, unit=unit, horizon=horizon,
                                    count=count, empirical_coverage=covered, mean_exceedance=excess,
                                    mean_margin=margin))
    return dict(schema_version=1, generated_at_epoch_ms=now_ms, retention_days=retention_days,
                evaluation_role="realized_selected_forecast", horizon_origin="data_end_ms",
                coverage=coverage, rows=rows, calibration_rows=calibrated_rows,
                shadow_comparison=shadow_report(db),
                activation_assessment=activation_assessment(db, now_ms, retention_days), ledger=DB_NAME)


def score_realized_forecasts(out_base: Path, items: Iterable[dict] = (), *, retention_days: int = 7,
                            publish_report: bool = True) -> dict:
    """Import new archives, backfill pending points, and atomically publish a report."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    base = Path(out_base)
    if not (base / "forecast_history").exists() and not (base / DB_NAME).exists():
        return {"status": "no_archives", "newly_scored": 0}
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - retention_days * 86400_000
    timings = {}
    with closing(sqlite3.connect(base / DB_NAME, timeout=30)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA temp_store=FILE")
        db.executescript(_SCHEMA)
        db.executescript(SHADOW_SCHEMA)
        started = time.perf_counter()
        imported = _import_archives(db, base, cutoff)
        timings["import_seconds"] = time.perf_counter()-started
        started = time.perf_counter()
        with db:
            db.execute("BEGIN IMMEDIATE")
            result = _score_evidence(db, items, now_ms)
        timings["score_seconds"] = time.perf_counter()-started
        started = time.perf_counter()
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM batches WHERE issued_ms<?", (cutoff,))
            report = _report(db, now_ms, retention_days) if publish_report else None
            if report is not None:
                atomic_write_json(base / REPORT_NAME, report, indent=2)
        timings["retention_report_seconds"] = time.perf_counter()-started
    return dict(status="completed", imported_batches=imported, **result,
                report_published=publish_report, timings=timings,
                coverage=report["coverage"] if report is not None else None)


def try_score_realized_forecasts(out_base: Path, items: Iterable[dict] = (), *, publish_report: bool = True) -> dict:
    """Scoring failure must not undo an already committed observation/forecast."""
    from resource_predict.settings import settings

    try:
        result = score_realized_forecasts(out_base, items, retention_days=settings.forecast.archive_retention_days,
                                         publish_report=publish_report)
    except Exception as exc:
        logger.warning("[forecast_realized] scoring failed: %s", exc)
        result = {"status": "failed", "error": str(exc)}
    logger.info("[forecast_realized] %s", result)
    return result


def main() -> None:
    from resource_predict.data.raw_store import RawResourceStore
    from resource_predict.settings import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    store = RawResourceStore(args.out_dir, max_cache_items=1)
    items = (store.get(rid) for rid in store.resource_ids()) if store.exists() else ()
    print(json.dumps(score_realized_forecasts(
        args.out_dir, items, retention_days=settings.forecast.archive_retention_days,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
