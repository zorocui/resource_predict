"""Reports page payloads in SQLite and summarize cached aggregate observations."""

import json
import re
from pathlib import Path
import sqlite3

import pytest

from resource_predict.services.scaling import effect_reports
from resource_predict.services.scaling.effect_metrics import evaluate_series, summarize_events


NOW = 1_000_000
SCHEMA = """
CREATE TABLE events (
 task_id TEXT PRIMARY KEY,resource_id TEXT,resource_type TEXT,action TEXT,status TEXT,
 started_ms INTEGER,payload TEXT,deadline_ms INTEGER
);
CREATE INDEX events_order ON events(started_ms,task_id);
CREATE TABLE metric_summaries (
 task_id TEXT,metric TEXT,basis TEXT,unit TEXT,before_pct REAL,after_pct REAL,
 relative_change_pct REAL,before_capacity REAL,reclaimed_capacity REAL,reclaimed_unit_hours REAL
);
"""


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(effect_reports.time, "time", lambda: NOW / 1000)


@pytest.fixture(autouse=True)
def legacy_sql(monkeypatch):
    connect = effect_reports.sqlite3.connect

    class LegacyConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            assert not re.search(r"\bWITH\b|\bOVER\s*\(|\bON\s+CONFLICT\b", sql, re.I), sql
            return super().execute(sql, parameters)

    monkeypatch.setattr(effect_reports.sqlite3, "connect",
                        lambda *args, **kwargs: connect(*args, factory=LegacyConnection, **kwargs))


def metric(before_capacity=2, after_capacity=1, before_usage=1, after_usage=1, **labels):
    policy = {"before_hours": 1, "after_hours": 1, "stabilization_minutes": 0, "max_gap_ms": 3_600_000}
    rows = evaluate_series([{
        "container": "", "metric": "cpu", "basis": "capacity", "unit": "cores", **labels,
        "timestamps": [0, 3_600_000, 7_200_000], "usage": [before_usage, after_usage, 1],
        "capacity": [before_capacity, after_capacity, 1],
    }], started_at_ms=3_600_000, effective_at_ms=3_600_000, policy=policy, now_ms=7_200_000)
    return next(row for row in rows if not row["container"])


def event(task_id, row=None, **values):
    return {"task_id": task_id, "resource_id": "resource", "resource_type": "openstack_vm",
            "action": "scale_in", "status": "evaluated", "started_at_ms": 1,
            "reason": "original", "policy": {"version": 1}, "metrics": [row] if row else [], **values}


def database(directory: Path, events):
    directory.mkdir()
    with sqlite3.connect(directory / effect_reports.DB_NAME) as db:
        db.executescript(SCHEMA)
        for item in events:
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
                item["task_id"], item["resource_id"], item["resource_type"], item["action"], item["status"],
                item["started_at_ms"], json.dumps(item), item.get("deadline_ms", NOW + 10_000),
            ))
            for row in item["metrics"]:
                if item["status"] != "evaluated" or row["container"] or row["status"] != "evaluated":
                    continue
                db.execute("INSERT INTO metric_summaries VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    item["task_id"], row["metric"], row["basis"], row["unit"],
                    row["before"]["utilization_pct"], row["after"]["utilization_pct"], row["relative_change_pct"],
                    row["before"]["mean_capacity"], row["reclaimed_capacity"], row["reclaimed_unit_hours"],
                ))
    return directory


def test_sql_summary_matches_pure_math_across_scopes_and_repeated_resources(tmp_path):
    cpu = metric()
    negative = metric(8, 16, 4, 4)
    zero = metric(before_usage=0)
    memory = metric(metric="memory", unit="GiB")
    left = [event("cpu", cpu), event("negative", negative), event("zero", zero),
            event("memory", memory), event("fail", cpu, status="failed"),
            event("expand", negative, action="scale_out")]
    right = [event("cpu", cpu, resource_type="k8s_workload"),
             event("more", cpu, resource_id="another-resource")]
    directories = [database(tmp_path / "vm", left), database(tmp_path / "k8s", right)]
    report = effect_reports.outcome_report(directories, page_size=2)
    expected = summarize_events(left + right)
    assert report["summary"] == expected
    assert report["total"] == len(left + right)
    assert len(report["items"]) == 2
    # Same resource ID and type across different files is still counted once.
    assert report["summary"]["resource_count"] == 3


def test_paging_limits_candidate_queries_and_decodes_only_selected_payloads(tmp_path, monkeypatch):
    aggregate = metric()
    bulky_metrics = [aggregate] + [{**aggregate, "container": f"container-{number}", "detail": "x" * 500}
                                  for number in range(40)]
    entries = [event(f"task-{number:03}", started_at_ms=number, metrics=bulky_metrics) for number in range(80)]
    directory = database(tmp_path / "scope", entries)
    # A historical payload never requested by this page need not be deserialized.
    with sqlite3.connect(directory / effect_reports.DB_NAME) as db:
        db.execute("UPDATE events SET payload='not json' WHERE task_id='task-000'")
    traces, decoded = [], []
    connect = sqlite3.connect
    loads = json.loads

    def traced_connection(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(traces.append)
        return db

    def counted_loads(payload):
        decoded.append(payload)
        return loads(payload)

    monkeypatch.setattr(effect_reports.sqlite3, "connect", traced_connection)
    monkeypatch.setattr(effect_reports.json, "loads", counted_loads)
    report = effect_reports.outcome_report([directory], page=2, page_size=5)
    assert [item["task_id"] for item in report["items"]] == [f"task-{n:03}" for n in range(74, 69, -1)]
    assert len(decoded) == 5
    assert all(len(item["metrics"]) == 1 for item in report["items"])
    candidates = [query for query in traces if query.startswith("SELECT task_id,started_ms")]
    assert len(candidates) == 1
    assert "LIMIT 10" in candidates[0]
    payload_queries = [query for query in traces if query.startswith("SELECT payload")]
    assert len(payload_queries) == 5
    assert all("WHERE task_id=" in query for query in payload_queries)
    assert report["summary"]["metrics"][0]["event_count"] == 80


def test_global_page_orders_scope_candidates_and_ties(tmp_path):
    left = [event("a", started_at_ms=100), event("c", started_at_ms=90), event("old", started_at_ms=10)]
    right = [event("b", started_at_ms=100), event("d", started_at_ms=90), event("mid", started_at_ms=50)]
    directories = [database(tmp_path / "left", left), database(tmp_path / "right", right)]
    report = effect_reports.outcome_report(directories, page=2, page_size=2)
    assert [item["task_id"] for item in report["items"]] == ["d", "c"]
    assert [item["task_id"] for item in effect_reports.list_events(directories)] == ["b", "a", "d", "c", "mid", "old"]


def test_derived_expiration_is_consistent_in_filters_counts_and_csv_without_mutation(tmp_path):
    entries = [event("late", status="observing", deadline_ms=NOW),
               event("ready", metric(), deadline_ms=NOW - 1),
               event("failed", status="failed", deadline_ms=NOW - 1),
               event("future", status="observing", deadline_ms=NOW + 1),
               event("unknown", status="unknown_state", deadline_ms=NOW - 1)]
    directory = database(tmp_path / "scope", entries)
    expired = effect_reports.outcome_report([directory], status="expired")
    assert expired["total"] == 2
    assert expired["summary"]["status_counts"] == {"expired": 2}
    assert expired["summary"]["metrics"] == []
    assert all(item["status"] == "expired" and item["reason"] != "original" for item in expired["items"])
    assert effect_reports.list_events([directory], status="expired") == expired["items"]
    assert effect_reports.outcome_report([directory], status="observing")["total"] == 1
    assert effect_reports.outcome_report([directory], status="evaluated")["total"] == 1
    assert effect_reports.outcome_report([directory], status="failed")["total"] == 1
    with sqlite3.connect(directory / effect_reports.DB_NAME) as db:
        assert db.execute("SELECT status FROM events WHERE task_id='late'").fetchone()[0] == "observing"


def test_all_filters_apply_to_page_summary_and_full_export(tmp_path):
    entries = [event("needle-task", metric(), resource_id="other"),
               event("id", metric(), resource_id="NEEDLE-resource"),
               event("different", metric(), resource_type="k8s_workload", resource_id="needle"),
               event("expand", metric(), resource_id="needle", action="scale_out"),
               event("irrelevant", metric())]
    directory = database(tmp_path / "scope", entries)
    filters = {"resource_type": "openstack_vm", "action": "scale_in", "status": "evaluated", "q": "Needle"}
    report = effect_reports.outcome_report([directory], **filters)
    assert report["total"] == 2
    assert report["summary"]["metrics"][0]["event_count"] == 2
    assert report["items"] == effect_reports.list_events([directory], **filters)
    assert {item["task_id"] for item in report["items"]} == {"needle-task", "id"}
    assert effect_reports.outcome_report([directory], q="' OR 1=1 --")["total"] == 0


def test_empty_missing_scope_and_out_of_range_page_do_not_create_files(tmp_path):
    missing = tmp_path / "missing"
    report = effect_reports.outcome_report([missing])
    assert report["items"] == []
    assert report["summary"] == {"event_count": 0, "resource_count": 0, "status_counts": {}, "metrics": []}
    assert not missing.exists()
    assert effect_reports.list_events([missing]) == []
    directory = database(tmp_path / "present", [event("one", metric())])
    report = effect_reports.outcome_report([missing, directory, directory], page=10, page_size=2)
    assert report["total"] == 1
    assert report["items"] == []
    assert report["summary"]["metrics"][0]["event_count"] == 1


def test_report_databases_are_read_only(tmp_path):
    directory = database(tmp_path / "scope", [])
    with effect_reports._databases([directory]) as (db, _):
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM events")
