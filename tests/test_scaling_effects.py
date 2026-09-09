import csv
import hashlib
import io
import json
import time
from types import SimpleNamespace

from flask import Flask
import pandas as pd

from resource_predict.api.scaling_effects import register_scaling_effect_routes
from resource_predict.services.scaling import effects, tasks

HOUR = 3600000
START = int(time.time() * 1000) - 2 * HOUR
POLICY = {"before_hours": 1, "after_hours": 1, "stabilization_minutes": 0, "max_gap_ms": HOUR // 4}


def evidence(start, end, capacity, *, usage=1, collected=None):
    times = list(range(start, end + 1, HOUR // 4))
    return {"schema_version": 1, "source": "prometheus_unfilled", "collected_at_ms": end if collected is None else collected,
            "spec": {"cpu_cores": capacity}, "sample_interval_ms": HOUR // 4,
            "series": [{"container": "", "metric": "cpu", "basis": "capacity", "unit": "cores",
                        "timestamps": times, "usage": [usage] * len(times), "capacity": [capacity] * len(times)}]}


def begin(path, task_id="task-1", resource_id="vm-1"):
    task = {"task_id": task_id, "resource_id": resource_id, "resource_type": "openstack_vm", "mode": "execute"}
    effects.start_event(path, task, {"cpu_cores": 4}, {"cpu_cores": 2}, now_ms=START, policy=POLICY,
                        evidence=evidence(START-HOUR, START, 4))
    return task


def finish(path, task_id="task-1", resource_id="vm-1", *, usage=1):
    effects.complete_event(path, task_id, success=True, now_ms=START + 1)
    effects.ingest_evidence(path, [{"resource_id": resource_id, "scaling_evidence": evidence(START+HOUR, START+2*HOUR, 2, usage=usage)}], now_ms=START+2*HOUR)
    return effects.event_evidence([path], task_id)["event"]


def test_end_to_end_real_window_and_reproducible_evidence(tmp_path):
    begin(tmp_path)
    event = finish(tmp_path)
    assert event["status"] == "evaluated"
    metric = event["metrics"][0]
    assert metric["before"]["utilization_pct"] == 25
    assert metric["after"]["utilization_pct"] == 50
    assert metric["delta_pp"] == 25
    assert metric["relative_change_pct"] == 100
    assert metric["reclaimed_unit_hours"] == 2
    payload = effects.event_evidence([tmp_path], "task-1")
    digest = payload.pop("sha256")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert hashlib.sha256(canonical.encode()).hexdigest() == digest
    assert payload["event"]["series"][0]["usage"]


def test_dry_run_not_recorded_and_success_not_effective(tmp_path):
    effects.start_event(tmp_path, {"mode": "dry_run"}, {}, {})
    assert not (tmp_path / effects.DB_NAME).exists()
    begin(tmp_path)
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    event = effects.event_evidence([tmp_path], "task-1")["event"]
    assert event["status"] == "awaiting_effective"
    assert event["effective_at_ms"] is None
    assert event["metrics"] == []


def test_failure_is_not_counted_as_improvement(tmp_path):
    begin(tmp_path)
    effects.complete_event(tmp_path, "task-1", success=False, now_ms=START+1)
    report = effects.outcome_report([tmp_path])
    assert report["summary"]["status_counts"] == {"failed": 1}
    assert report["summary"]["metrics"] == []


def test_repeated_tasks_and_evidence_do_not_change_frozen_outcome(tmp_path):
    begin(tmp_path)
    begin(tmp_path)
    event = finish(tmp_path)
    finish(tmp_path, usage=100)
    assert effects.outcome_report([tmp_path])["total"] == 1
    assert effects.event_evidence([tmp_path], "task-1")["event"]["metrics"] == event["metrics"]


def test_new_execution_interrupts_open_event(tmp_path):
    begin(tmp_path)
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    effects.start_event(tmp_path, {"task_id": "task-2", "resource_id": "vm-1", "resource_type": "openstack_vm", "mode": "execute"},
                        {"cpu_cores": 2}, {"cpu_cores": 4}, now_ms=START+HOUR)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "interrupted"


def test_missing_before_window_stays_insufficient(tmp_path):
    effects.start_event(tmp_path, {"task_id": "task-1", "resource_id": "vm-1", "resource_type": "openstack_vm", "mode": "execute"},
                        {"cpu_cores": 4}, {"cpu_cores": 2}, now_ms=START, policy=POLICY)
    event = finish(tmp_path)
    assert event["status"] == "insufficient_data"
    assert event["metrics"][0]["relative_change_pct"] is None


def test_actual_capacity_drift_excludes_outcome(tmp_path):
    begin(tmp_path)
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    post = evidence(START+HOUR, START+2*HOUR, 2)
    post["series"][0]["capacity"][2] = 3
    effects.ingest_evidence(tmp_path, [{"resource_id": "vm-1", "scaling_evidence": post}], now_ms=START+2*HOUR)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "basis_changed"
    assert effects.event_evidence([tmp_path], "task-1")["event"]["metrics"][0]["relative_change_pct"] is None
    assert effects.outcome_report([tmp_path])["summary"]["metrics"] == []


def test_conflicting_evidence_is_not_silently_overwritten(tmp_path):
    begin(tmp_path)
    changed = evidence(START-HOUR, START, 4, usage=2)
    effects.ingest_evidence(tmp_path, [{"resource_id": "vm-1", "scaling_evidence": changed}], now_ms=START)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "evidence_conflict"


def test_api_filters_export_pagination_and_empty_state(tmp_path):
    app = Flask(__name__)
    register_scaling_effect_routes(app, lambda: [tmp_path])
    client = app.test_client()
    assert client.get("/api/scaling-effects").get_json()["total"] == 0
    assert not (tmp_path / effects.DB_NAME).exists()
    begin(tmp_path, resource_id="=untrusted")
    finish(tmp_path, resource_id="=untrusted", usage=.25)
    begin(tmp_path, task_id="task-2", resource_id="other")
    assert client.get("/api/scaling-effects?page_size=1").get_json()["total"] == 2
    filtered = client.get("/api/scaling-effects?q=task-1&status=evaluated").get_json()
    assert filtered["total"] == 1
    assert filtered["summary"]["metrics"][0]["mean_relative_change_pct"] < 0
    exported = client.get("/api/scaling-effects/export.csv?q=task-1&status=evaluated")
    rows = list(csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["resource_id"] == "'=untrusted"
    assert float(rows[0]["relative_change_pct"]) == -50
    assert client.get("/api/scaling-effects/task-1/evidence.json").status_code == 200
    assert client.get("/api/scaling-effects/no-such-event").status_code == 404
    assert client.get("/api/scaling-effects?page=0").status_code == 400
    assert client.get("/api/scaling-effects?action=invalid").status_code == 400
    assert client.get(f"/api/scaling-effects?from_ms={START}&to_ms={START+1}").get_json()["total"] == 2
    assert client.get(f"/api/scaling-effects?from_ms={START+1}").get_json()["total"] == 0
    assert client.get(f"/api/scaling-effects/export.csv?to_ms={START}").data.decode("utf-8-sig").count("\n") == 1
    assert client.get("/api/scaling-effects?from_ms=2&to_ms=1").status_code == 400


def test_real_task_capture_and_manual_confirm_completion_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_PATH", tmp_path / "scaling_tasks.json")
    plan = SimpleNamespace(resource_id="vm-1", resource_type="openstack_vm", target_spec={"cpu_cores": 2}, details={})
    tasks._upsert_task({"task_id": "real", "resource_id": "vm-1", "mode": "execute", "status": "running",
                        "plan": {"resource_type": "openstack_vm"}})
    tasks._capture_effect("real", {"resource_id": "vm-1", "resource_type": "openstack_vm", "spec": {"cpu_cores": 4}}, plan)
    tasks._patch_task("real", {"status": "waiting_confirm"})
    assert effects.event_evidence([tmp_path / "vm"], "real")["event"]["completed_at_ms"] is None
    tasks._patch_task("real", {"status": "success"})
    assert effects.event_evidence([tmp_path / "vm"], "real")["event"]["status"] == "awaiting_effective"


def test_malformed_baseline_keeps_durable_failed_capture(tmp_path):
    bad = evidence(START-HOUR, START, 4)
    bad["series"][0]["capacity"] = []
    effects.start_event(tmp_path, {"task_id": "bad", "resource_id": "vm-1", "resource_type": "openstack_vm", "mode": "execute"},
                        {"cpu_cores": 4}, {"cpu_cores": 2}, now_ms=START, policy=POLICY, evidence=bad)
    event = effects.event_evidence([tmp_path], "bad")["event"]
    assert event["status"] == "capture_failed"
    assert "aligned" in event["capture_error"]


def test_delayed_evidence_is_not_interrupted_after_window_end(tmp_path):
    begin(tmp_path)
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    short = evidence(START+HOUR, START+HOUR+HOUR//4, 2, collected=START+2*HOUR)
    effects.ingest_evidence(tmp_path, [{"resource_id": "vm-1", "scaling_evidence": short}], now_ms=START+2*HOUR)
    effects.start_event(tmp_path, {"task_id": "next", "resource_id": "vm-1", "resource_type": "openstack_vm", "mode": "execute"},
                        {"cpu_cores": 2}, {"cpu_cores": 4}, now_ms=START+3*HOUR)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "insufficient_data"
    effects.ingest_evidence(tmp_path, [{"resource_id": "vm-1", "scaling_evidence": evidence(START+HOUR, START+2*HOUR, 2)}], now_ms=START+3*HOUR)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "evaluated"


def test_partial_cpu_does_not_freeze_missing_memory(tmp_path):
    before = evidence(START-HOUR, START, 4)
    before["spec"]["memory_gb"] = 4
    before["series"].append({**before["series"][0], "metric": "memory", "unit": "GiB"})
    effects.start_event(tmp_path, {"task_id": "task-1", "resource_id": "vm-1", "resource_type": "openstack_vm", "mode": "execute"},
                        {"cpu_cores": 4, "memory_gb": 4}, {"cpu_cores": 2, "memory_gb": 2}, now_ms=START, policy=POLICY, evidence=before)
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    post = evidence(START+HOUR, START+2*HOUR, 2)
    post["spec"]["memory_gb"] = 2
    post["series"].append({"container": "", "metric": "memory", "basis": "capacity", "unit": "GiB",
                            "timestamps": [START+HOUR], "usage": [1], "capacity": [2]})
    effects.ingest_evidence(tmp_path, [{"resource_id": "vm-1", "scaling_evidence": post}], now_ms=START+2*HOUR)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "insufficient_data"
    post["series"][1] = {**post["series"][0], "metric": "memory", "unit": "GiB"}
    effects.ingest_evidence(tmp_path, [{"resource_id": "vm-1", "scaling_evidence": post}], now_ms=START+2*HOUR)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "evaluated"


def test_unresolved_evidence_stops_after_deadline(tmp_path):
    begin(tmp_path)
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    deadline = effects.event_evidence([tmp_path], "task-1")["event"]["deadline_ms"]
    effects.ingest_evidence(tmp_path, [], now_ms=deadline+1)
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "expired"


def test_k8s_departed_replica_history_does_not_replace_frozen_baseline(tmp_path):
    old_spec = {"replicas": 2, "containers": {"app": {"cpu_request_cores": 4}}}
    before = evidence(START-HOUR, START, 8, usage=2)
    before["spec"] = old_spec
    before["series"][0].update(container="app", basis="request")
    task = {"task_id": "k8s-task", "resource_id": "workload", "resource_type": "k8s_workload", "mode": "execute"}
    effects.start_event(tmp_path, task, old_spec, {"replicas": 1}, now_ms=START, policy=POLICY, evidence=before)
    effects.complete_event(tmp_path, "k8s-task", success=True, now_ms=START+1)
    post = evidence(START-HOUR, START+2*HOUR, 4, usage=2)
    post["spec"] = {**old_spec, "replicas": 1}
    post["series"][0].update(container="app", basis="request")
    effects.ingest_evidence(tmp_path, [{"resource_id": "workload", "scaling_evidence": post}], now_ms=START+2*HOUR)
    event = effects.event_evidence([tmp_path], "k8s-task")["event"]
    assert event["status"] == "evaluated"
    aggregate = next(m for m in event["metrics"] if not m["container"])
    assert aggregate["before"]["utilization_pct"] == 25
    assert aggregate["after"]["utilization_pct"] == 50


def test_k8s_same_capacity_product_is_not_target_spec_confirmation(tmp_path):
    old = {"replicas": 1, "containers": {"app": {"cpu_request_cores": 8}}}
    task = {"task_id": "k8s-task", "resource_id": "workload", "resource_type": "k8s_workload", "mode": "execute"}
    effects.start_event(tmp_path, task, old, {"replicas": 2, "containers": {"app": {"cpu_request_cores": 2}}}, now_ms=START, policy=POLICY)
    effects.complete_event(tmp_path, "k8s-task", success=True, now_ms=START+1)
    wrong = evidence(START+HOUR, START+2*HOUR, 4)
    wrong["spec"] = {"replicas": 1, "containers": {"app": {"cpu_request_cores": 4}}}
    wrong["series"][0].update(container="app", basis="request")
    effects.ingest_evidence(tmp_path, [{"resource_id": "workload", "scaling_evidence": wrong}], now_ms=START+2*HOUR)
    assert effects.event_evidence([tmp_path], "k8s-task")["event"]["effective_at_ms"] is None


def test_effectiveness_proof_survives_stabilization_exclusion(tmp_path):
    task = {"task_id": "task-1", "resource_id": "vm-1", "resource_type": "openstack_vm", "mode": "execute"}
    effects.start_event(tmp_path, task, {"cpu_cores": 4}, {"cpu_cores": 2}, now_ms=START,
                        policy={**POLICY, "stabilization_minutes": 60}, evidence=evidence(START-HOUR, START, 4))
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    effects.ingest_evidence(tmp_path, [{"resource_id": "vm-1", "scaling_evidence": evidence(START+HOUR, START+3*HOUR, 2)}], now_ms=START+3*HOUR)
    event = effects.event_evidence([tmp_path], "task-1")["event"]
    assert event["effective_confirmation"]["timestamp_ms"] == START+HOUR
    assert event["effective_confirmation"]["points"][0]["capacity"] == 2
    assert event["metrics"][0]["after"]["start_ms"] == START+2*HOUR


def test_raw_store_roundtrip_and_commit_hook_preserve_independent_evidence(tmp_path):
    from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset

    begin(tmp_path)
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    observed = evidence(START+HOUR, START+2*HOUR, 2)
    series = pd.Series([.5, .5], index=pd.to_datetime([START+HOUR, START+2*HOUR], unit="ms"))
    resource = {"resource_id": "vm-1", "resource_type": "openstack_vm", "spec": {"cpu_cores": 2},
                "scaling_evidence": observed, "cpu": series, "memory": series, "disk": series}
    write_raw_resource_dataset(tmp_path, [resource], freq="1h")
    restored = RawResourceStore(tmp_path).get("vm-1")
    assert restored["scaling_evidence"] == observed
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "evaluated"


def test_bad_resource_does_not_rollback_other_resources(tmp_path):
    begin(tmp_path, resource_id="good")
    begin(tmp_path, task_id="bad-task", resource_id="bad")
    effects.complete_event(tmp_path, "task-1", success=True, now_ms=START+1)
    effects.complete_event(tmp_path, "bad-task", success=True, now_ms=START+1)
    bad = evidence(START+HOUR, START+2*HOUR, 2)
    bad["series"][0]["usage"] = []
    effects.ingest_evidence(tmp_path, [
        {"resource_id": "bad", "scaling_evidence": bad},
        {"resource_id": "good", "scaling_evidence": evidence(START+HOUR, START+2*HOUR, 2)},
    ], now_ms=START+2*HOUR)
    assert effects.event_evidence([tmp_path], "bad-task")["event"]["status"] == "capture_failed"
    assert effects.event_evidence([tmp_path], "task-1")["event"]["status"] == "evaluated"
