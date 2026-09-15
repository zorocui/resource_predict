from flask import Flask

from resource_predict.api.scaling import register_scaling_routes
from resource_predict.services.scaling import tasks
from resource_predict.services.store.query import safe_int


def test_global_history_paginates_resources_and_preserves_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_PATH", tmp_path / "scaling_tasks.json")
    monkeypatch.setattr(tasks.threading.Thread, "start", lambda self: None)
    resource = {"resource_id": "vm-a", "resource_type": "openstack_vm", "spec": {"cpu_cores": 4}}
    created = tasks.create_scaling_task(resource)
    resource["spec"]["cpu_cores"] = 16
    tasks._patch_task(created["task_id"], {"status": "success", "updated_at_ms": 100,
                                        "plan": {"target_spec": {"cpu_cores": 8}, "commands": ["private-command"]}})
    rows = tasks._read_tasks()
    rows[0]["created_at_ms"] = 10
    rows.append({"task_id": "legacy", "resource_id": "k8s:b", "created_at_ms": 20, "mode": "execute",
                 "status": "failed", "results": [{"stdout": "private-output"}]})
    tasks._write_tasks(rows)
    app = Flask(__name__)
    register_scaling_routes(app, {"get_resource_detail": lambda *_args, **_kwargs: None, "safe_int": safe_int})
    client = app.test_client()
    first = client.get("/api/scaling-history?page_size=1").get_json()
    assert first["total"] == 2
    assert first["items"][0]["resource_id"] == "k8s:b"
    assert first["items"][0]["before_spec"] is None
    second = client.get("/api/scaling-history?page_size=1&page=2").get_json()["items"][0]
    assert second["before_spec"] == {"cpu_cores": 4}
    assert second["target_spec"] == {"cpu_cores": 8}
    assert second["finished_at_ms"] == 100
    assert "commands" not in str(second) and "private-output" not in str(first)
    filtered = client.get("/api/scaling-history?q=VM-A").get_json()
    assert filtered["total"] == 1
    assert client.get("/api/scaling-history?page=-1&page_size=999").get_json()["page_size"] == 100
    assert client.get("/api/scaling-history?q=missing").get_json()["items"] == []
