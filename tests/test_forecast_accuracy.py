"""Accuracy contract: publication selection, denominators, units and read-only export."""
import json
import sqlite3

import pytest

from resource_predict.pipeline.realized_error import _SCHEMA
from resource_predict.services.forecast_accuracy import DB_NAME, accuracy_session


@pytest.fixture
def ledger(tmp_path):
    db = sqlite3.connect(tmp_path / DB_NAME)
    db.executescript(_SCHEMA)
    yield db, tmp_path
    db.close()


def add(db, *, rid="vm", model="a", issued=1000, target=3600000,
        predicted=.5, actual=.5, unit="openstack_vm:ratio", container="",
        reason=None, eligible=1, source="prometheus", data_end=0):
    batch = f"batch-{db.execute('SELECT count(*) FROM curves').fetchone()[0]}"
    db.execute("INSERT INTO batches VALUES (?,?)", (batch, issued))
    kind = "k8s_workload" if unit.startswith("k8s_workload") else "openstack_vm"
    cur = db.execute("INSERT INTO curves(batch,resource_id,container,metric,model,unit,data_end_ms,issued_ms,basis,provenance,eligible) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (batch, rid, container, "cpu", model, unit, data_end, issued, json.dumps([kind, {}]), "{}", eligible))
    db.execute("INSERT INTO points(curve_id,target_ms,predicted,actual,observation_source,skip_reason) VALUES (?,?,?,?,?,?)",
               (cur.lastrowid, target, predicted, actual, source, reason))
    db.commit()


def test_absent_database_is_read_only(tmp_path):
    with accuracy_session([tmp_path]) as session:
        assert session.report()["total"] == 0
        assert list(session.points()) == []
    assert list(tmp_path.iterdir()) == []


def test_dedup_precedes_model_filter_and_never_cherry_picks(ledger):
    db, path = ledger
    add(db, model="old", issued=1000)
    add(db, model="new", issued=2000, actual=None)
    with accuracy_session([path], now_ms=4000000) as session:
        report = session.report()
        assert report["coverage"]["candidate_points"] == 2
        assert report["coverage"]["duplicate_points"] == 1
        assert report["coverage"]["matched_points"] == 0
        assert report["items"][0]["model"] == "new"
    with accuracy_session([path], model="old", now_ms=4000000) as session:
        assert session.report()["total"] == 0


def test_boundaries_zero_macro_micro_and_nearest_rank(ledger):
    db, path = ledger
    add(db, rid="zero", target=10000, predicted=0, actual=0)
    add(db, rid="zero", target=20000, predicted=.55, actual=.5)
    add(db, rid="zero", target=30000, predicted=.6, actual=.5)
    add(db, rid="other", target=10000, predicted=.2, actual=0)
    with accuracy_session([path], now_ms=4000000) as session:
        row = session.report()["summary"][0]
        assert row["count"] == 4 and row["resource_count"] == 2
        assert row["hit_rate_5pp"] == .5
        assert row["hit_rate_10pp"] == .75
        assert row["macro_hit_rate_5pp"] == pytest.approx(1/3)
        assert row["macro_hit_rate_10pp"] == .5
        assert row["mae"] == pytest.approx(8.75)
        assert row["p95_error"] == 20
        assert row["unit"] == "percentage_points"
        assert session.report()["coverage"]["observation_coverage"] == 1


def test_invalid_points_are_not_zero_and_due_denominator_is_complete(ledger):
    db, path = ledger
    cases = [dict(rid="missing", actual=None), dict(rid="basis", reason="basis_mismatch"),
             dict(rid="infinite", actual=float("inf")), dict(rid="mock", source="mock"),
             dict(rid="provenance", eligible=0), dict(rid="past", target=1000),
             dict(rid="future", target=9000000), dict(rid="unknown", unit="k8s_workload:unknown")]
    for case in cases:
        add(db, **case)
    add(db, rid="zero", predicted=0, actual=0)
    with accuracy_session([path], now_ms=4000000) as session:
        report = session.report()
        assert report["coverage"]["due_points"] == 8
        assert report["coverage"]["matched_points"] == 1
        assert report["coverage"]["observation_coverage"] == .125
        assert report["coverage"]["nonfinite_observation"] == 1
        for point in session.points():
            if point["status"] != "matched":
                assert point["error"] is None and point["hit_5pp"] is None
        json.dumps(report, allow_nan=False)


def test_units_container_cohorts_and_target_pagination(ledger):
    db, path = ledger
    for unit in ("k8s_workload:cpu_usage/cpu_request", "k8s_workload:cpu_usage/cpu_limit", "k8s_workload:cpu_usage_cores"):
        for container in ("one", "two"):
            add(db, rid="workload", container=container, unit=unit, target=10000)
    with accuracy_session([path], from_ms=10000, to_ms=10001, level="container", now_ms=20000) as session:
        report = session.report(page=2, page_size=2)
        points = list(session.points())
        assert len(report["summary"]) == 3
        assert report["items"] == points[2:4]
        assert len(points) == 6
        for row in report["summary"]:
            assert row["resource_count"] == 1 and row["cohort_count"] == 2
            if row["unit"] == "cores":
                assert row["hit_rate_5pp"] is None
    with accuracy_session([path], to_ms=10000, now_ms=20000) as session:
        assert session.report()["total"] == 0


def test_holdout_model_evidence_is_independent_and_latest_fold_wins(ledger):
    db, path = ledger
    for cid, model, end, actual, role in [(1, "a", 1000, .5, "independent_test"),
                                         (2, "a", 2000, None, "independent_test"),
                                         (3, "b", 2000, .5, "independent_test"),
                                         (4, "c", 2000, .5, "validation")]:
        db.execute("INSERT INTO holdout_curves VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, str(cid), "vm", "", "cpu", model, "openstack_vm:ratio", end,
                    50000, '["openstack_vm",{}]', '{}', json.dumps(dict(role=role)), 1))
        db.execute("INSERT INTO holdout_points VALUES (?,?,?,?,?)", (cid, 10000, .5, actual, None))
    db.commit()
    with accuracy_session([path], source="holdout", now_ms=100000) as session:
        report = session.report()
        assert report["total"] == 3
        assert report["coverage"]["matched_points"] == 1
        assert report["summary"][0]["model"] == "b"
        assert report["coverage"]["missing_provenance"] == 1
        assert report["coverage"]["duplicate_points"] == 1
    with accuracy_session([path], source="holdout", model="a", now_ms=100000) as session:
        assert session.report()["items"][0]["actual"] is None


def test_report_and_export_share_read_snapshot_and_cannot_write(ledger):
    db, path = ledger
    db.execute("PRAGMA journal_mode=WAL")
    add(db, actual=None)
    with accuracy_session([path], now_ms=4000000) as session:
        assert session.report()["coverage"]["matched_points"] == 0
        db.execute("UPDATE points SET actual=.5")
        db.commit()
        assert list(session.points())[0]["actual"] is None
        with pytest.raises(sqlite3.OperationalError):
            session.db.execute("DELETE FROM ledger_0.points")
    with accuracy_session([path], now_ms=4000000) as session:
        assert session.report()["coverage"]["matched_points"] == 1


def test_missing_holdout_tables_do_not_mutate_legacy_database(ledger):
    db, path = ledger
    db.executescript("DROP TABLE holdout_points; DROP TABLE holdout_curves;")
    before = db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    with accuracy_session([path], source="holdout") as session:
        assert session.report()["total"] == 0
        assert any("absent" in warning for warning in session.report()["warnings"])
    assert db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall() == before


def test_frozen_holdout_failures_are_not_waiting_for_future_observations(ledger):
    db, path = ledger
    for cid, predicted, actual, reason in [(1, .5, None, "invalid_observation"), (2, None, .5, "invalid_prediction")]:
        db.execute("INSERT INTO holdout_curves VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            cid, f"b{cid}", f"vm{cid}", "", "cpu", "a", "openstack_vm:ratio", 1000, 5000,
            '["openstack_vm",{}]', '{}', '{"role":"independent_test"}', 1))
        db.execute("INSERT INTO holdout_points VALUES (?,?,?,?,?)", (cid, 3000, predicted, actual, reason))
    db.commit()
    with accuracy_session([path], source="holdout", now_ms=6000) as session:
        report = session.report()
        assert report["coverage"]["awaiting_observation"] == 0
        assert report["coverage"]["invalid_prediction"] == 1
        assert report["coverage"]["nonfinite_observation"] == 1
        assert {row["skip_reason"] for row in session.points()} == {"invalid_observation", "invalid_prediction"}


def test_horizons_use_publication_not_training_cutoff(ledger):
    db, path = ledger
    for index, delta in enumerate((0, 1, 3600000, 3600001, 21600000, 21600001, 86400000, 86400001)):
        add(db, rid=str(index), issued=10000000, data_end=1, target=10000000+delta)
    with accuracy_session([path], now_ms=200000000) as session:
        assert [p["horizon"] for p in session.points()] == ["unknown", "0-1h", "0-1h", "1-6h", "1-6h", "6-24h", "6-24h", ">24h"]
