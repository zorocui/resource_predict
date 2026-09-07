import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from resource_predict.pipeline.activation_assessment import activation_assessment
from resource_predict.pipeline.realized_error import _SCHEMA, DB_NAME, REPORT_NAME, score_realized_forecasts
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


def test_report_entry_empty_evidence_and_no_automatic_activation(tmp_path):
    with closing(sqlite3.connect(tmp_path/DB_NAME)) as db:
        db.executescript(_SCHEMA+SHADOW_SCHEMA)
        assert activation_assessment(db,NOW,7)["status"] == "no_shadow_evidence"
        seed(db)
    with patch("time.time",return_value=NOW/1000):
        score_realized_forecasts(tmp_path)
    first = json.loads((tmp_path/REPORT_NAME).read_text())["activation_assessment"]
    with patch("time.time",return_value=NOW/1000):
        score_realized_forecasts(tmp_path)
    second = json.loads((tmp_path/REPORT_NAME).read_text())["activation_assessment"]
    assert first == second
    assert first["automatic_activation"] is False
    assert first["resources"][0]["status"] == "eligible_for_review"


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
