import os
from threading import Event

import numpy as np
import pandas as pd
import pytest

from resource_predict.pipeline import parallel, plan
from resource_predict.pipeline._types import WorkerContext
from resource_predict.settings import settings


def context():
    return WorkerContext(test_size=4, future_steps=2, active_methods=["rolling_mean"],
                         forecast_config={}, metric_filter_by_id={}, metric_partial_enabled=False,
                         existing_partial_ids=set(), sample_interval_seconds=3600)


def jobs(count):
    for i in range(count):
        yield i, "", "cpu", pd.Series(np.arange(40, dtype=float) + i,
                                      index=pd.date_range("2026-01-01", periods=40, freq="h"))


def test_plan_cpu_and_backend_selection(monkeypatch):
    monkeypatch.setattr(plan, "available_cpu_count", lambda: 8)
    assert plan.resolve_execution_plan(100, ["sarima"])["workers"] == 7
    assert plan.resolve_execution_plan(100, ["sarima"])["backend"] == "process"
    assert plan.resolve_execution_plan(100, ["rolling_mean"])["backend"] == "thread"
    assert plan.resolve_execution_plan(2, ["arima"], max_workers=99)["workers"] == 2
    assert plan.resolve_execution_plan(10, ["arima"], max_workers=1)["backend"] == "serial"
    assert plan.resolve_execution_plan(0, ["arima"])["workers"] == 1
    monkeypatch.setattr(plan, "available_cpu_count", lambda: 100)
    monkeypatch.setattr(plan.sys, "platform", "win32")
    assert plan.resolve_execution_plan(1000, ["arima"])["workers"] == 61


@pytest.mark.parametrize("value", [-1, True, 1.5, "2", None])
def test_plan_rejects_invalid_workers(value):
    with pytest.raises(ValueError, match="max_workers"):
        plan.resolve_execution_plan(10, [], max_workers=value)


def test_cpu_affinity_and_nested_cgroup_quota(monkeypatch):
    files = {"/proc/self/cgroup": "0::/service/job",
             "/sys/fs/cgroup/service/cpu.max": "300000 100000",
             "/sys/fs/cgroup/service/job/cpu.max": "max 100000"}
    monkeypatch.setattr(plan, "_read_text", lambda path: files.get(path.as_posix(), ""))
    monkeypatch.setattr(plan.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(plan.os, "sched_getaffinity", lambda _: set(range(6)), raising=False)
    monkeypatch.setattr(plan.sys, "platform", "linux")
    assert plan.available_cpu_count() == 3
    files["/sys/fs/cgroup/service/cpu.max"] = "50000 100000"
    assert plan.available_cpu_count() == 1
    files.clear()
    files.update({"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "200000",
                  "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"})
    assert plan.available_cpu_count() == 2


def test_thread_submission_is_bounded_and_failure_visible(monkeypatch):
    consumed = []
    release = Event()

    def inputs():
        for i in range(100):
            consumed.append(i)
            if len(consumed) == 4:
                release.set()
            yield i

    def fail(job, ctx, snapshot):
        assert release.wait(5)
        raise RuntimeError("metric failed")

    monkeypatch.setattr(parallel, "_run_metric", fail)
    stats = {}
    execution = {"backend": "thread", "workers": 2, "max_in_flight": 4}
    with pytest.raises(RuntimeError, match="metric failed"):
        list(parallel.execute_metric_jobs(inputs(), None, None, execution, stats))
    assert len(consumed) == stats["submitted"] == stats["max_in_flight"] == 4
    assert stats["completed"] == 0


def test_spawn_matches_serial_and_uses_child_processes():
    snapshot = settings.freeze()
    ctx = context()
    ctx.active_methods = ["arima", "rolling_mean"]
    outputs = {}
    for backend in ("serial", "thread", "process"):
        stats = {}
        execution = {"backend": backend, "workers": 2, "max_in_flight": 4}
        outputs[backend] = sorted(parallel.execute_metric_jobs(jobs(8), ctx, snapshot,
                                                               execution, stats), key=lambda row: row[0])
        assert stats["completed"] == stats["submitted"] == 8
        assert stats["max_in_flight"] <= 4
        if backend == "process":
            assert len(stats["pids"]) == 2 and os.getpid() not in stats["pids"]
    for baseline, threaded, spawned in zip(outputs["serial"], outputs["thread"], outputs["process"]):
        for candidate in (threaded, spawned):
            assert candidate[:3] == baseline[:3]
            for index in (0, 3):
                for method, expected in baseline[3][index].items():
                    pd.testing.assert_series_equal(candidate[3][index][method], expected)
            assert candidate[3][1] == baseline[3][1]
            assert candidate[3][2] == baseline[3][2]
            assert candidate[3][5]["provenance"]["config_hash"] == baseline[3][5]["provenance"]["config_hash"]


def test_spawn_failure_propagates_without_fallback():
    invalid = pd.Series([], index=pd.DatetimeIndex([]), dtype=float)
    execution = {"backend": "process", "workers": 2, "max_in_flight": 4}
    with pytest.raises(ValueError, match="test endpoint"):
        list(parallel.execute_metric_jobs([(0, "", "cpu", invalid)], context(),
                                          settings.freeze(), execution, {}))
