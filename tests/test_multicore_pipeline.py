import json
import os
from unittest.mock import patch

import numpy as np
import pandas as pd

from resource_predict.pipeline.run import generate_forecasts
from resource_predict.settings import settings


def test_single_workload_container_tasks_spawn_and_keep_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("resource_predict.pipeline.plan.available_cpu_count", lambda: 2)
    step = settings.k8s_prometheus.step_seconds
    values = pd.Series(.1 + .02*np.sin(np.arange(72)),
                       index=pd.date_range("2026-01-01", periods=72, freq=f"{step}s"))
    names = ("cpu_limit", "cpu_request", "memory_limit", "memory_request")
    source = {
        "resource_id": "k8s:qa:ns:deployment:parallel", "resource_type": "k8s_workload",
        "spec": {"cluster": "qa", "namespace": "ns", "replicas": 1, "workload_kind": "Deployment",
                 "containers": {name: {"cpu_request_cores": 1, "cpu_limit_cores": 2,
                                       "memory_request_gb": 1, "memory_limit_gb": 2} for name in ("app", "sidecar")}},
        "metrics": {name: values for name in names},
        "container_metrics": {container: {name: values for name in names} for container in ("app", "sidecar")},
    }
    cfg = {"enabled_methods": ["rolling_mean"], "enable_ensemble": False}
    with patch("resource_predict.pipeline.run.read_forecast_config", return_value=cfg):
        generate_forecasts(out_dir=str(tmp_path), data_provider=lambda **_: [source],
                           test_size=6, future_steps=3, parallel_backend="process", max_workers=2, save_raw=False)
    report = json.loads((tmp_path / "generation_stats.json").read_text(encoding="utf-8"))
    execution = report["execution"]
    assert execution["backend"] == "process"
    assert execution["task_count"] == execution["completed"] == 12
    assert execution["max_in_flight"] <= 4
    assert execution["pids"] and os.getpid() not in execution["pids"]
    assert execution["assembly_seconds"] >= 0 and execution["output_write_seconds"] >= 0
    detail = json.loads((tmp_path / "details" / "part-00000.json").read_text(encoding="utf-8"))["resources"][0]
    assert set(detail["container_charts_forecast"]) == {"app", "sidecar"}
    assert "_accuracy_holdout" not in detail
    summary = json.loads((tmp_path / "forecast_accuracy_summary.json").read_text(encoding="utf-8"))
    assert len(summary["rows"]) == 8
    assert not (tmp_path / "forecast_history").exists()
    assert not (tmp_path / "forecast_realized.sqlite3").exists()
