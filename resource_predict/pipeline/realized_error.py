"""Legacy evidence helpers and reports; archive/import entry points were removed."""
from __future__ import annotations

import json
import logging
import math
from resource_predict.sqlite_runtime import sqlite3

from resource_predict.resource_types import resource_type_of
from resource_predict.pipeline.shadow_evaluation import shadow_report
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
CREATE INDEX IF NOT EXISTS points_pending ON points(curve_id, actual, target_ms);
CREATE INDEX IF NOT EXISTS curves_batch ON curves(batch);
CREATE INDEX IF NOT EXISTS points_target ON points(target_ms,curve_id);
CREATE TABLE IF NOT EXISTS calibrations (
 curve_id INTEGER PRIMARY KEY REFERENCES curves(id) ON DELETE CASCADE,
 metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upper_bounds (
 curve_id INTEGER NOT NULL, target_ms INTEGER NOT NULL, upper REAL NOT NULL,
 PRIMARY KEY(curve_id, target_ms),
 FOREIGN KEY(curve_id,target_ms) REFERENCES points(curve_id,target_ms) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS holdout_curves (
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE,
 resource_id TEXT NOT NULL, container TEXT NOT NULL, metric TEXT NOT NULL,
 model TEXT NOT NULL, unit TEXT NOT NULL, data_end_ms INTEGER, issued_ms INTEGER NOT NULL,
 basis TEXT NOT NULL, provenance TEXT NOT NULL, evaluation TEXT NOT NULL, eligible INTEGER NOT NULL,
 UNIQUE(batch, resource_id, container, metric, model)
);
CREATE TABLE IF NOT EXISTS holdout_points (
 curve_id INTEGER NOT NULL REFERENCES holdout_curves(id) ON DELETE CASCADE,
 target_ms INTEGER NOT NULL, predicted REAL, actual REAL, skip_reason TEXT,
 PRIMARY KEY(curve_id, target_ms)
);
CREATE INDEX IF NOT EXISTS holdout_curves_batch ON holdout_curves(batch);
CREATE INDEX IF NOT EXISTS holdout_curves_lookup ON holdout_curves(resource_id,container,metric);
CREATE INDEX IF NOT EXISTS holdout_points_target ON holdout_points(target_ms,curve_id);
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




def _unit(item: dict, container: str, metric: str) -> str:
    kind = resource_type_of(item)
    if kind == "k8s_workload":
        mode = (item.get("container_metric_modes", {}).get(container, {}).get(metric) if container
                else item.get("spec", {}).get(f"{metric}_metric_mode"))
        return f"{kind}:{mode or 'unknown'}"
    return f"{kind}:ratio"










def _report(db: sqlite3.Connection, now_ms: int, retention_days: int) -> dict:
    coverage = dict(db.execute(
        "SELECT CASE WHEN actual IS NOT NULL THEN 'scored' WHEN skip_reason IS NOT NULL THEN skip_reason "
        "WHEN target_ms>? THEN 'awaiting_target' ELSE 'awaiting_observation' END, COUNT(*) "
        "FROM points GROUP BY 1", (now_ms,),
    ))
    rows = []
    # Reduce to curve/horizon totals before sorting by wide model/unit strings.
    for model, metric, level, horizon, unit, count, mae, mse, under, magnitude in db.execute(
        "SELECT c.model,c.metric,CASE WHEN c.container='' THEN 'resource' ELSE 'container' END, "
        "t.horizon,c.unit,SUM(t.n),SUM(t.ae)/SUM(t.n),SUM(t.se)/SUM(t.n),"
        "SUM(t.under)/SUM(t.n),SUM(t.magnitude)/SUM(t.n) "
        "FROM (SELECT p.curve_id,CASE WHEN p.target_ms-c.data_end_ms<=3600000 THEN '0-1h' "
        "WHEN p.target_ms-c.data_end_ms<=21600000 THEN '1-6h' "
        "WHEN p.target_ms-c.data_end_ms<=86400000 THEN '6-24h' ELSE '>24h' END AS horizon, "
        "COUNT(*) AS n,SUM(ABS(p.actual-p.predicted)) AS ae,"
        "SUM((p.actual-p.predicted)*(p.actual-p.predicted)) AS se,"
        "SUM(CASE WHEN p.actual>p.predicted THEN 1.0 ELSE 0.0 END) AS under,"
        "SUM(MAX(p.actual-p.predicted,0)) AS magnitude "
        "FROM points p JOIN curves c ON c.id=p.curve_id WHERE p.actual IS NOT NULL GROUP BY 1,2) "
        "t JOIN curves c ON c.id=t.curve_id GROUP BY 1,2,3,4,5 "
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
