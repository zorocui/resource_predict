import json
import re
import sqlite3
from contextlib import closing

import pytest

from resource_predict.pipeline.activation_assessment import activation_assessment
from resource_predict.pipeline.realized_error import _SCHEMA, DB_NAME
from resource_predict.pipeline.shadow_evaluation import SCHEMA as SHADOW_SCHEMA

NOW = 1_788_700_000_000
HOUR = 3600000


def encode(value):
    return json.dumps(value, sort_keys=True)


def seed(db, *, rid="vm-a", kind="openstack_vm", runs=16):
    db.executescript(_SCHEMA + SHADOW_SCHEMA)
    db.execute("PRAGMA foreign_keys=ON")
    if kind == "k8s_workload":
        spec = {"containers": {"app": {}, "sidecar": {}}}
        keys = [(c,m) for c in ("app","sidecar") for m in ("cpu_request","cpu_limit","memory_request","memory_limit")]
    else:
        spec = {"cpu_cores": 10,"memory_gb": 20,"disk_gb": 100}
        keys = [("",m) for m in ("cpu","memory","disk")]
    baseline = {"action": "scale_in","policy_tier": "balanced","target_spec": spec}
    candidate = {"action": "scale_in","policy_tier": "balanced","target_spec": {"test_allocation": 0.8}}
    with db:
        for number in range(runs):
            issued = NOW-(runs-number)*6*HOUR-1000
            batch = f"{rid}-batch-{number:03d}"
            db.execute("INSERT INTO batches VALUES (?,?)",(batch,issued+1))
            snapshot = {"version": 1,"executable": False,"source_spec": spec}
            run_id = db.execute(
                "INSERT INTO shadow_runs(batch,resource_id,resource_type,basis,status,snapshot,baseline,candidate) VALUES (?,?,?,?,?,?,?,?)",
                (batch,rid,kind,encode(spec),"paired",encode(snapshot),encode(baseline),encode(candidate)),
            ).lastrowid
            for container,metric in keys:
                curve_id = db.execute(
                    "INSERT INTO curves(batch,resource_id,container,metric,model,unit,data_end_ms,issued_ms,basis,provenance,eligible) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                    (batch,rid,container,metric,"rolling_mean","ratio",issued,issued+1,encode(spec),
                     encode({"model_version": "v1","config_hash": "cfg"})),
                ).lastrowid
                role = "request_budget" if metric.endswith("request") else "capacity"
                db.execute("INSERT INTO shadow_budgets VALUES (?,?,?,?,?,?,?,?,?)",
                           (curve_id,run_id,"cores" if metric.startswith("cpu") else "GiB",role,10,8,1,0.8,None))
                for p in range(12):
                    target = issued+(p+1)*HOUR//2
                    db.execute("INSERT INTO points(curve_id,target_ms,predicted,actual,scored_at_ms) VALUES (?,?,?,?,?)",
                               (curve_id,target,0.3,0.4,target+100))


@pytest.fixture
def db(tmp_path):
    with closing(sqlite3.connect(tmp_path/DB_NAME)) as connection:
        seed(connection)
        yield connection


def assess(db, now=NOW):
    return activation_assessment(db,now,7)["resources"][0]


def test_ready_is_review_only_and_preserves_database(db):
    changes = db.total_changes
    result = activation_assessment(db,NOW,7)
    item, = result["resources"]
    assert item["status"] == "eligible_for_review"
    assert item["reasons"] == []
    assert item["paired_runs"] == 16
    assert item["stability"]["transitions"] == 15
    assert all(m["matched_targets"] == 192 for m in item["metrics"])
    assert all(m["reservation_reduction"] == pytest.approx(0.2) for m in item["metrics"])
    assert item["valid_until_epoch_ms"] > NOW
    assert result["automatic_activation"] is False
    assert result["mode"] == "review_only"
    assert db.total_changes == changes


@pytest.mark.parametrize("column,expected", [("actual", "observation_coverage"), ("scored_at_ms", "sample_count")])
def test_unscored_and_future_scored_points_are_not_evidence(db,column,expected):
    with db:
        if column == "actual":
            db.execute("UPDATE points SET actual=NULL WHERE rowid%2=0")
        else:
            db.execute("UPDATE points SET scored_at_ms=?",(NOW+1,))
    result = assess(db)
    assert result["status"] == "continue_observing"
    assert expected in result["metrics"][0]["failed_checks"]


def test_increased_risk_in_one_metric_blocks_whole_resource(db):
    with db:
        db.execute("UPDATE points SET actual=0.9 WHERE curve_id IN (SELECT id FROM curves WHERE metric='memory')")
    result = assess(db)
    assert result["status"] == "continue_observing"
    memory = next(m for m in result["metrics"] if m["metric"] == "memory")
    assert memory["baseline_rate"] == 0
    assert memory["shadow_rate"] == 1
    assert "exceedance_rate" in memory["failed_checks"]


def test_no_benefit_or_allocation_increase_blocks(db):
    with db:
        db.execute("UPDATE shadow_budgets SET shadow_allocation=baseline_allocation,shadow_ratio=baseline_ratio")
    assert "insufficient_reservation_benefit" in assess(db)["reasons"]
    with db:
        db.execute("UPDATE shadow_budgets SET shadow_allocation=8 WHERE curve_id IN (SELECT id FROM curves WHERE metric='cpu')")
        db.execute("UPDATE shadow_budgets SET shadow_allocation=11 WHERE curve_id IN (SELECT id FROM curves WHERE metric='disk')")
    result = assess(db)
    assert "allocation_not_increased" in next(m for m in result["metrics"] if m["metric"]=="disk")["failed_checks"]


@pytest.mark.parametrize("change", ["unavailable","basis","model","config","policy"])
def test_changed_regime_resets_continuous_evidence(db,change):
    run_id,batch = db.execute("SELECT id,batch FROM shadow_runs ORDER BY id DESC LIMIT 1 OFFSET 2").fetchone()
    with db:
        if change == "unavailable":
            db.execute("UPDATE shadow_runs SET status='unavailable' WHERE id=?",(run_id,))
        elif change == "basis":
            db.execute("UPDATE shadow_runs SET basis='changed' WHERE id=?",(run_id,))
        elif change == "model":
            db.execute("UPDATE curves SET model='arima' WHERE batch=?",(batch,))
        elif change == "config":
            db.execute("UPDATE curves SET provenance=? WHERE batch=?",(encode({"model_version":"v1","config_hash":"new"}),batch))
        else:
            candidate = json.loads(db.execute("SELECT candidate FROM shadow_runs WHERE id=?",(run_id,)).fetchone()[0])
            candidate["policy_tier"] = "aggressive"
            db.execute("UPDATE shadow_runs SET candidate=? WHERE id=?",(encode(candidate),run_id))
    result = assess(db)
    assert result["paired_runs"] == 2
    assert "insufficient_continuous_paired_runs" in result["reasons"]


def test_fresh_publish_does_not_hide_stale_forecast_data(db):
    with db:
        db.execute("UPDATE curves SET data_end_ms=? WHERE batch=(SELECT batch FROM shadow_runs ORDER BY id DESC LIMIT 1)",
                   (NOW-25*HOUR,))
    assert assess(db)["reasons"] == ["stale_or_future_forecast_data"]


def test_stale_and_future_latest_run_do_not_qualify(db):
    assert assess(db,NOW+25*HOUR)["reasons"] == ["stale_or_future_prediction"]
    with db:
        db.execute("UPDATE batches SET issued_ms=? WHERE name=(SELECT batch FROM shadow_runs ORDER BY id DESC LIMIT 1)",(NOW+1,))
    assert assess(db)["reasons"] == ["stale_or_future_prediction"]


def test_recent_missing_observations_cannot_be_hidden_by_old_history(db):
    with db:
        db.execute("UPDATE points SET actual=NULL WHERE target_ms>?",(NOW-25*HOUR,))
    result = assess(db)
    assert "fresh_observations" in result["metrics"][0]["failed_checks"]


def test_increased_change_rate_blocks(db):
    with db:
        for run_id,candidate in db.execute("SELECT id,candidate FROM shadow_runs WHERE id%2=0").fetchall():
            changed = json.loads(candidate)
            changed["action"] = "hold"
            db.execute("UPDATE shadow_runs SET candidate=? WHERE id=?",(encode(changed),run_id))
    result = assess(db)
    assert result["stability"]["candidate_changes"] == 15
    assert result["stability"]["baseline_changes"] == 0
    assert "recommendation_changes_increased" in result["reasons"]


def test_deduplication_precedes_observation_filter(db):
    # Add an overlapping target to a later genuine run, with no observation yet.
    metric = "cpu"
    later = db.execute("SELECT id,issued_ms FROM curves WHERE metric=? ORDER BY issued_ms LIMIT 1 OFFSET 1",(metric,)).fetchone()
    earlier = db.execute("SELECT id FROM curves WHERE metric=? ORDER BY issued_ms LIMIT 1",(metric,)).fetchone()[0]
    target = later[1]+HOUR//4
    with db:
        db.execute("INSERT INTO points(curve_id,target_ms,predicted,actual,scored_at_ms) VALUES (?,?,?,?,?)",
                   (earlier,target,0.3,0.4,target+100))
        db.execute("INSERT INTO points(curve_id,target_ms,predicted) VALUES (?,?,?)",(later[0],target,0.3))
    cpu = next(m for m in assess(db)["metrics"] if m["metric"]==metric)
    assert cpu["due_targets"] == 193
    assert cpu["matched_targets"] == 192


def test_resources_and_containers_are_not_pooled(db):
    seed(db,rid="k8s-a",kind="k8s_workload")
    with db:
        db.execute("UPDATE points SET actual=0.9 WHERE curve_id IN (SELECT id FROM curves WHERE resource_id='k8s-a' AND container='sidecar' AND metric='cpu_limit')")
    result = activation_assessment(db,NOW,7)
    by_id = {r["resource_id"]:r for r in result["resources"]}
    assert by_id["vm-a"]["status"] == "eligible_for_review"
    assert by_id["k8s-a"]["status"] == "continue_observing"
    assert len(by_id["k8s-a"]["metrics"]) == 8


def test_k8s_limit_reduction_alone_is_not_reservation_benefit(tmp_path):
    with closing(sqlite3.connect(tmp_path/DB_NAME)) as db:
        seed(db,rid="k8s-a",kind="k8s_workload")
        with db:
            db.execute("UPDATE shadow_budgets SET shadow_allocation=baseline_allocation,shadow_ratio=baseline_ratio WHERE role='request_budget'")
        assert "insufficient_reservation_benefit" in assess(db)["reasons"]




@pytest.mark.parametrize("missing,eligible",[(9,True),(10,False)])
def test_observation_coverage_threshold(db,missing,eligible):
    with db:
        db.execute("UPDATE points SET actual=NULL WHERE target_ms IN "
                   "(SELECT DISTINCT target_ms FROM points ORDER BY target_ms LIMIT ?)",(missing,))
    result = assess(db)
    assert (result["status"]=="eligible_for_review") == eligible
    assert result["metrics"][0]["observation_coverage"] == pytest.approx((192-missing)/192)


def test_same_exceedance_rate_but_worse_magnitude_is_blocked(db):
    with db:
        db.execute("UPDATE points SET actual=1.2")
    result = assess(db)
    assert result["metrics"][0]["checks"]["exceedance_rate"] is True
    assert result["metrics"][0]["checks"]["excess_magnitude"] is False
    assert result["status"]=="continue_observing"


def test_latest_invalid_and_missing_budget_never_qualify(db):
    with db:
        db.execute("UPDATE shadow_budgets SET skip_reason='replicas_changed' WHERE run_id=(SELECT MAX(id) FROM shadow_runs)")
    assert assess(db)["reasons"] == ["incomplete_or_incomparable_latest_budgets"]
    with db:
        db.execute("UPDATE shadow_budgets SET skip_reason=NULL")
        db.execute("DELETE FROM shadow_budgets WHERE curve_id=(SELECT MAX(curve_id) FROM shadow_budgets)")
    assert assess(db)["reasons"] == ["incomplete_or_incomparable_latest_budgets"]


def test_execution_recheck_can_limit_to_requested_resource(db):
    seed(db,rid="vm-b")
    result=activation_assessment(db,NOW,7,resource_ids=("vm-b",))
    assert [row["resource_id"] for row in result["resources"]]==["vm-b"]
    assert activation_assessment(db,NOW,7,resource_ids=())["status"]=="no_shadow_evidence"


def test_shadow_stream_matches_window_reference_across_resource_and_regime_boundaries(db):
    from resource_predict.pipeline.shadow_evaluation import shadow_report

    seed(db, rid="vm-b")
    seed(db, rid="k8s-a", kind="k8s_workload")
    with db:
        db.execute("UPDATE shadow_runs SET status='unavailable',snapshot=? WHERE id%5=0",
                   (encode({"reason": "missing_evidence"}),))
        db.execute("UPDATE shadow_runs SET basis='changed' WHERE id%7=0")
        db.execute("UPDATE shadow_runs SET candidate='changed' WHERE id%3=0")
        db.execute("UPDATE shadow_runs SET baseline='changed' WHERE id%4=0")
    # A modern-SQL oracle only in this test protects the former LAG semantics.
    expected = list(db.execute(
        "SELECT resource_type,COUNT(*),SUM(baseline!=prev_baseline),SUM(candidate!=prev_candidate) FROM ("
        "SELECT r.*,LAG(baseline) OVER w AS prev_baseline,LAG(candidate) OVER w AS prev_candidate,"
        "LAG(basis) OVER w AS prev_basis,LAG(status) OVER w AS prev_status "
        "FROM shadow_runs r JOIN batches b ON b.name=r.batch "
        "WINDOW w AS (PARTITION BY resource_type,resource_id ORDER BY b.issued_ms,r.batch)) "
        "WHERE status='paired' AND prev_status='paired' AND basis=prev_basis GROUP BY resource_type"
    ))
    report = shadow_report(db)
    actual = [(row["resource_type"], row["comparable_transitions"], row["baseline_changes"],
               row["shadow_changes"]) for row in report["change_rows"]]
    assert actual == expected
    assert report["unavailable_reasons"] == {"missing_evidence": 9}


class LegacySQLGuard:
    """Reject newer SQL and binding counts before modern SQLite can accept them."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        self.check(sql)
        assert len(parameters) <= 999
        return self.connection.execute(sql, parameters)

    def executescript(self, sql):
        self.check(sql)
        return self.connection.executescript(sql)

    def create_function(self, name, count, function):
        # Deliberately lacks the unsupported deterministic keyword.
        return self.connection.create_function(name, count, function)

    @staticmethod
    def check(sql):
        assert not re.search(r"\b(?:WITH|OVER|WINDOW|RETURNING|json_\w+)\b", sql, re.I)
        assert not re.search(r"ON\s+CONFLICT.*DO\s+", sql, re.I | re.S)
        assert not re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX[^;]*\bWHERE\b", sql, re.I)


def test_metric_evidence_accepts_more_than_999_internal_run_ids(db):
    from resource_predict.pipeline.activation_assessment import _metric_evidence

    # Existing IDs produce evidence; the additional genuine INTEGER IDs stress
    # the selection size independently of the size of the point fixtures.
    db.executemany("INSERT INTO batches VALUES (?,?)", ((f"extra-{i}", NOW) for i in range(1000)))
    db.executemany(
        "INSERT INTO shadow_runs(batch,resource_id,resource_type,basis,status,snapshot) VALUES (?,?,?,?,?,?)",
        ((f"extra-{i}", "vm-a", "openstack_vm", "{}", "unavailable", "{}") for i in range(1000)),
    )
    run_ids = [row[0] for row in db.execute("SELECT id FROM shadow_runs")]
    if hasattr(db, "setlimit"):
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    result = _metric_evidence(LegacySQLGuard(db), run_ids, "", "cpu", NOW)
    assert len(run_ids) > 999
    assert result["matched_targets"] == result["due_targets"] == 192
    assert result["reservation_reduction"] == pytest.approx(0.2)
    for invalid in ("1) OR 1=1 --", 1.0, True):
        with pytest.raises(ValueError, match="must be integers"):
            _metric_evidence(LegacySQLGuard(db), [invalid], "", "cpu", NOW)


def test_pipeline_reports_and_calibration_use_legacy_sql(db):
    from resource_predict.pipeline.calibration import _calibrate_curve
    from resource_predict.pipeline.realized_error import _basis, _report, _unit

    legacy = LegacySQLGuard(db)
    legacy.executescript(_SCHEMA + SHADOW_SCHEMA)
    report = _report(legacy, NOW, 7)
    assert report["activation_assessment"]["resources"][0]["status"] == "eligible_for_review"
    assert report["shadow_comparison"]["change_rows"][0]["comparable_transitions"] == 15
    assert sum(row["count"] for row in report["rows"]) == 576
    source = {"resource_id": "vm-a", "resource_type": "openstack_vm", "spec": {}}
    db.execute("UPDATE curves SET basis=?,unit=?", (_basis(source, "", "cpu"), _unit(source, "", "cpu")))
    chart = {"best_method": "rolling_mean", "x_pred_ms": [NOW+1000],
             "preds_future": {"rolling_mean": [0.3]}}
    diagnostics = {"provenance": {"model_version": "v1", "config_hash": "cfg",
                                  "generated_at_epoch_ms": NOW, "data_end_ms": NOW}}
    calibration = _calibrate_curve(legacy, source, "", "cpu", chart, diagnostics, 7)
    assert calibration["buckets"][0]["sample_count"] == 32
    assert calibration["status"] == "insufficient_samples"
