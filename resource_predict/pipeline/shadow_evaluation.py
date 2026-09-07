"""SQLite storage and paired summaries for frozen shadow recommendations."""
import json


SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_runs (
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE,
 resource_id TEXT NOT NULL, resource_type TEXT NOT NULL, basis TEXT NOT NULL,
 status TEXT NOT NULL, snapshot TEXT NOT NULL, baseline TEXT, candidate TEXT,
 UNIQUE(batch,resource_id)
);
CREATE INDEX IF NOT EXISTS shadow_runs_resource ON shadow_runs(resource_id);
CREATE INDEX IF NOT EXISTS shadow_runs_batch ON shadow_runs(batch);
CREATE TABLE IF NOT EXISTS shadow_budgets (
 curve_id INTEGER PRIMARY KEY REFERENCES curves(id) ON DELETE CASCADE,
 run_id INTEGER NOT NULL REFERENCES shadow_runs(id) ON DELETE CASCADE,
 unit TEXT NOT NULL, role TEXT NOT NULL,
 baseline_allocation REAL, shadow_allocation REAL,
 baseline_ratio REAL, shadow_ratio REAL, skip_reason TEXT
);
CREATE INDEX IF NOT EXISTS shadow_budgets_run ON shadow_budgets(run_id);
"""


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def import_shadow(db, batch, record):
    snapshot = record.get("shadow_comparison")
    if not isinstance(snapshot, dict):
        return
    if snapshot.get("version") != 1 or snapshot.get("executable") is not False:
        raise ValueError("invalid shadow comparison")
    cursor = db.execute(
        "INSERT INTO shadow_runs(batch,resource_id,resource_type,basis,status,snapshot,baseline,candidate) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (batch, record["resource_id"], record.get("resource_type", "openstack_vm"),
         _encode([record.get("spec", {}), record.get("container_metric_modes", {})]), snapshot["status"],
         _encode(snapshot), _encode(snapshot.get("baseline")), _encode(snapshot.get("candidate"))),
    )
    run_id = cursor.lastrowid
    for row in snapshot.get("budgets", []):
        curve = db.execute("SELECT id FROM curves WHERE batch=? AND resource_id=? AND container=? AND metric=?",
                           (batch, record["resource_id"], row["container"], row["metric"])).fetchone()
        if curve is None:
            raise ValueError("shadow budget has no archived forecast")
        db.execute("INSERT INTO shadow_budgets VALUES (?,?,?,?,?,?,?,?,?)",
                   (curve[0], run_id, row["unit"], row["role"], row["baseline_allocation"], row["shadow_allocation"],
                    row["baseline_ratio"], row["shadow_ratio"], row["skip_reason"]))


def shadow_report(db):
    allocation_rows = []
    for kind, metric, unit, role, count, baseline, candidate in db.execute(
        "SELECT r.resource_type,c.metric,b.unit,b.role,COUNT(*),AVG(b.baseline_allocation),AVG(b.shadow_allocation) "
        "FROM shadow_budgets b JOIN curves c ON c.id=b.curve_id JOIN shadow_runs r ON r.id=b.run_id "
        "WHERE b.baseline_allocation IS NOT NULL AND b.shadow_allocation IS NOT NULL "
        "GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"
    ):
        allocation_rows.append(dict(resource_type=kind, metric=metric, unit=unit, role=role, pairs=count,
                                    mean_baseline_allocation=baseline, mean_shadow_allocation=candidate,
                                    mean_allocation_delta=candidate-baseline))
    actual_rows = []
    for kind, metric, unit, role, count, before, after, before_excess, after_excess in db.execute(
        "SELECT r.resource_type,c.metric,b.unit,b.role,COUNT(*),"
        "AVG(CASE WHEN p.actual>b.baseline_ratio THEN 1.0 ELSE 0.0 END),"
        "AVG(CASE WHEN p.actual>b.shadow_ratio THEN 1.0 ELSE 0.0 END),"
        "AVG(MAX(p.actual-b.baseline_ratio,0)),AVG(MAX(p.actual-b.shadow_ratio,0)) "
        "FROM shadow_budgets b JOIN curves c ON c.id=b.curve_id JOIN shadow_runs r ON r.id=b.run_id "
        "JOIN points p ON p.curve_id=c.id WHERE p.actual IS NOT NULL AND b.skip_reason IS NULL "
        "AND b.baseline_ratio IS NOT NULL AND b.shadow_ratio IS NOT NULL "
        "GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"
    ):
        actual_rows.append(dict(resource_type=kind, metric=metric, allocation_unit=unit, role=role,
                               matched_points=count, baseline_exceedance_rate=before,
                               shadow_exceedance_rate=after, baseline_mean_excess_ratio=before_excess,
                               shadow_mean_excess_ratio=after_excess))
    changes = []
    for kind, count, before, after in db.execute(
        "SELECT resource_type,COUNT(*),SUM(baseline!=prev_baseline),SUM(candidate!=prev_candidate) FROM ("
        "SELECT r.*,LAG(baseline) OVER w AS prev_baseline,LAG(candidate) OVER w AS prev_candidate,"
        "LAG(basis) OVER w AS prev_basis,LAG(status) OVER w AS prev_status "
        "FROM shadow_runs r JOIN batches b ON b.name=r.batch "
        "WINDOW w AS (PARTITION BY resource_type,resource_id ORDER BY b.issued_ms,r.batch)) "
        "WHERE status='paired' AND prev_status='paired' AND basis=prev_basis GROUP BY resource_type"
    ):
        changes.append(dict(resource_type=kind, comparable_transitions=count,
                            baseline_changes=before, shadow_changes=after,
                            baseline_change_rate=before/count, shadow_change_rate=after/count))
    return {"mode": "shadow", "executable": False,
            "run_counts": dict(db.execute("SELECT status,COUNT(*) FROM shadow_runs GROUP BY status")),
            "unavailable_reasons": dict(db.execute(
                "SELECT json_extract(snapshot,'$.reason'),COUNT(*) FROM shadow_runs WHERE status!='paired' GROUP BY 1")),
            "budget_skip_reasons": dict(db.execute(
                "SELECT skip_reason,COUNT(*) FROM shadow_budgets WHERE skip_reason IS NOT NULL GROUP BY skip_reason")),
            "allocation_rows": allocation_rows, "actual_rows": actual_rows, "change_rows": changes}
