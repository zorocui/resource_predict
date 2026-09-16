import threading
from itertools import count

import pytest

from resource_predict.data import updater
from resource_predict.services import k8s_ingest


@pytest.fixture
def isolated_updates(monkeypatch):
    monkeypatch.setattr(updater, "_update_status", dict(updater.get_update_status()))
    monkeypatch.setattr(updater, "_last_history_started_at", None)
    records = []
    monkeypatch.setattr(updater, "append_update_history", lambda record, **_kwargs: records.append(record) or True)
    lock = threading.Lock()
    monkeypatch.setattr(updater, "_update_exclusive", lock)
    monkeypatch.setattr(k8s_ingest, "_update_exclusive", lock)
    return lock, records


def test_scheduled_fetch_waits_without_overwriting_manual_task(monkeypatch, isolated_updates):
    lock, records = isolated_updates
    ticks = count(1000)
    monkeypatch.setattr(updater.time, "time", lambda: next(ticks))
    waiting = threading.Event()
    fetched = threading.Event()

    class ObservedLock:
        def acquire(self, **kwargs):
            waiting.set()
            return lock.acquire(**kwargs)

        def release(self):
            lock.release()

    monkeypatch.setattr(k8s_ingest, "_update_exclusive", ObservedLock())
    monkeypatch.setattr(k8s_ingest, "_history_hours_for_fetch", lambda **_kwargs: 7)

    def fetch(*args, **kwargs):
        state = updater.get_update_status()
        assert state["task_source"] == "K8S 后台定时拉取"
        assert state["last_finished_at"] is None
        assert lock.locked()
        fetched.set()
        return {"items": [{"resource_id": "workload"}], "cluster_results": []}

    def merge(**kwargs):
        assert kwargs["task_source"] == "K8S 后台定时拉取"
        assert kwargs["_exclusive_already_acquired"]
        assert kwargs["keep_running"]
        assert not kwargs["record_history"]
        assert lock.locked()
        return {"success": True}

    monkeypatch.setattr(k8s_ingest, "fetch_k8s_prometheus_result", fetch)
    monkeypatch.setattr(updater, "_do_update", merge)
    errors = []

    def run():
        try:
            k8s_ingest.run_k8s_prometheus_upsert(trigger_source="scheduled")
        except BaseException as exc:
            errors.append(exc)

    lock.acquire()
    updater.mark_external_update_started("predicting", "manual", metadata={"task_source": "单 Workload 重新拉取预测"})
    before = updater.get_update_status()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert waiting.wait(2)
        assert not fetched.is_set()
        assert updater.get_update_status() == before
        updater.mark_external_update_finished({"success": True})
    finally:
        lock.release()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert fetched.is_set()
    assert [r["task_source"] for r in records] == ["单 Workload 重新拉取预测", "K8S 后台定时拉取"]
    assert not updater.get_update_status()["running"]
    assert not lock.locked()


@pytest.mark.parametrize("entry", [
    lambda: k8s_ingest.run_k8s_prometheus_upsert(fail_if_busy=True),
    lambda: updater.run_scoped_upsert_with_data([], fail_if_busy=True),
])
def test_busy_entry_preserves_current_status(entry, isolated_updates):
    lock, records = isolated_updates
    updater.mark_external_update_started("predicting", "manual", metadata={"task_source": "单 Workload 重新拉取预测"})
    before = updater.get_update_status()
    with lock, pytest.raises(updater.UpdateBusyError):
        entry()
    assert updater.get_update_status() == before
    assert not records


def test_new_update_clears_previous_finish_before_processing(isolated_updates):
    _, records = isolated_updates
    updater._update_status.update(running=False, last_finished_at=100, task_source="旧任务")

    class InspectList(list):
        def __len__(self):
            assert updater.get_update_status()["last_finished_at"] is None
            return 0

    result = updater._do_update(new_data_list=InspectList(), task_source="推送 Upsert 更新")
    assert not result["success"]
    assert len(records) == 1
    assert records[0]["finished_at"] >= records[0]["started_at"]


def test_fetch_failure_releases_lock_and_records_own_source(monkeypatch, isolated_updates):
    lock, records = isolated_updates
    monkeypatch.setattr(k8s_ingest, "_history_hours_for_fetch", lambda **_kwargs: 7)

    def fail(*args, **kwargs):
        raise RuntimeError("fetch failed")

    monkeypatch.setattr(k8s_ingest, "fetch_k8s_prometheus_result", fail)
    with pytest.raises(RuntimeError, match="fetch failed"):
        k8s_ingest.run_k8s_prometheus_upsert(trigger_source="scheduled")
    assert not lock.locked()
    assert not updater.get_update_status()["running"]
    assert len(records) == 1
    assert records[0]["task_source"] == "K8S 后台定时拉取"
