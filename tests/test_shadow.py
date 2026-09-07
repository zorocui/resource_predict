import copy
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import numpy as np
import pytest

from resource_predict.core.decision import build_scaling_advice
from resource_predict.pipeline.forecast_archive import archive_forecasts
from resource_predict.pipeline.realized_error import _basis, score_realized_forecasts, DB_NAME, REPORT_NAME
from resource_predict.pipeline.shadow import build_shadow_advice
from resource_predict.pipeline.partial import merge_partial_forecast_items

T = 1_788_600_000_000


def vm():
    source = {"resource_id": "vm-a", "resource_type": "openstack_vm",
              "spec": {"cpu_cores": 8, "memory_gb": 16, "disk_gb": 100},
              "history_coverage": {"span_hours": 168}, "charts_forecast": {}, "forecast_diagnostics": {}}
    for metric in ("cpu", "memory", "disk"):
        source["charts_forecast"][metric] = chart(source, "", metric)
        source["forecast_diagnostics"][metric] = provenance()
    source["scaling_advice"] = build_scaling_advice(
        {m: np.asarray([0.2, 0.2]) for m in ("cpu", "memory", "disk")},
        current_spec=source["spec"], history_coverage=source["history_coverage"],
    )
    return source


def provenance():
    return {"provenance": {"data_end_ms": T, "generated_at_epoch_ms": T,
                           "model_version": "v1", "config_hash": "cfg"}}


def chart(source, container, metric):
    return {"x_pred_ms": [T+3600000, T+7200000], "best_method": "rolling_mean",
            "preds_future": {"rolling_mean": [0.2, 0.2]}, "forecast_diagnostics": provenance(),
            "calibration": {"status": "calibrated", "upper": [0.6, 0.6], "basis": _basis(source, container, metric)}}


def archive_and_score(base, source, *, received=0.7, issued=T):
    with patch("time.time", return_value=issued/1000):
        archive_forecasts(base, [source])
    incoming = {"resource_id": source["resource_id"], "observation_evidence": {
        "schema_version": 1, "source": "test", "resource_type": source["resource_type"], "spec": source["spec"],
        "container_metric_modes": source.get("container_metric_modes", {}),
        "metrics": {m: {"timestamps": [T+3600000], "values": [received]} for m in source["charts_forecast"]},
        "container_metrics": {c: {m: {"timestamps": [T+3600000], "values": [received]} for m in charts}
                              for c, charts in source.get("container_charts_forecast", {}).items()},
    }}
    with patch("time.time", return_value=(T+10800000)/1000):
        score_realized_forecasts(base, [incoming])
        score_realized_forecasts(base, [incoming])
    return json.loads((base / REPORT_NAME).read_text())["shadow_comparison"]


def test_shadow_keeps_formal_advice_and_uses_existing_algorithm(tmp_path):
    source = vm()
    before = copy.deepcopy(source)
    build_shadow_advice([source])
    result = source["shadow_comparison"]
    assert result["status"] == "paired"
    assert result["executable"] is False
    assert source["scaling_advice"] == before["scaling_advice"]
    assert source["charts_forecast"] == before["charts_forecast"]
    expected = build_scaling_advice({m: np.asarray([0.6, 0.6]) for m in ("cpu", "memory", "disk")},
                                    current_spec=source["spec"], history_coverage=source["history_coverage"])
    assert result["candidate"]["target_spec"] == expected["target_spec"]
    assert "action_gate" not in result["candidate"]
    report = archive_and_score(tmp_path, source)
    assert report["run_counts"] == {"paired": 1}
    assert len(report["actual_rows"]) == 3
    assert all(row["matched_points"] == 1 for row in report["actual_rows"])
    budgets = {r["metric"]: r for r in result["budgets"]}
    for row in report["actual_rows"]:
        budget = budgets[row["metric"]]
        assert row["baseline_exceedance_rate"] == int(0.7 > budget["baseline_ratio"])
        assert row["shadow_exceedance_rate"] == int(0.7 > budget["shadow_ratio"])
    assert len(report["allocation_rows"]) == 3
    assert report["change_rows"] == []


@pytest.mark.parametrize("failure", ["partial", "missing", "basis", "nonfinite", "container"])
def test_incomplete_calibration_does_not_create_a_pair(failure):
    source = vm()
    if failure == "partial":
        source["charts_forecast"]["cpu"]["calibration"]["status"] = "partial"
    elif failure == "missing":
        del source["charts_forecast"]["memory"]
    elif failure == "basis":
        source["spec"]["cpu_cores"] = 4
    elif failure == "nonfinite":
        source["charts_forecast"]["cpu"]["calibration"]["upper"][0] = float("nan")
    else:
        source = workload()
        del source["container_charts_forecast"]["app"]["cpu_limit"]
    build_shadow_advice([source])
    assert source["shadow_comparison"]["status"] == "unavailable"
    assert "budgets" not in source["shadow_comparison"]


def workload():
    spec = {"containers": {"app": {"cpu_request_cores": 0.5, "cpu_limit_cores": 1,
                                    "memory_request_gb": 0.5, "memory_limit_gb": 1}}, "replicas_observed": 2}
    metrics = ("cpu_request", "cpu_limit", "memory_request", "memory_limit")
    source = {"resource_id": "k8s:a", "resource_type": "k8s_workload", "spec": spec,
              "container_metric_modes": {"app": {m: "usage/"+m for m in metrics}},
              "charts_forecast": {}, "forecast_diagnostics": {}, "container_charts_forecast": {"app": {}},
              "scaling_advice": {"action": "hold", "target_spec": {"containers": copy.deepcopy(spec["containers"]), "replicas": 2},
                                 "policy_tier": "balanced"}}
    for metric in metrics:
        source["charts_forecast"][metric] = chart(source, "", metric)
        source["forecast_diagnostics"][metric] = provenance()
        source["container_charts_forecast"]["app"][metric] = chart(source, "app", metric)
    return source


@pytest.mark.parametrize("replicas", [2, 3])
def test_container_budgets_and_replica_counterfactual_limit(tmp_path, replicas):
    source = workload()
    candidate = copy.deepcopy(source["scaling_advice"])
    candidate["target_spec"]["replicas"] = replicas
    candidate["target_spec"]["containers"]["app"]["cpu_request_cores"] = 0.25
    with patch("resource_predict.pipeline.shadow.build_k8s_workload_advice", return_value=candidate):
        build_shadow_advice([source])
    report = archive_and_score(tmp_path, source)
    request = next(r for r in report["allocation_rows"] if r["metric"] == "cpu_request")
    assert request["mean_baseline_allocation"] == 1
    assert request["mean_shadow_allocation"] == 0.25*replicas
    assert request["role"] == "request_budget"
    if replicas == 3:
        assert report["actual_rows"] == []
        assert report["budget_skip_reasons"] == {"replicas_changed": 4}
    else:
        row = next(r for r in report["actual_rows"] if r["metric"] == "cpu_request")
        assert row["baseline_exceedance_rate"] == 0
        assert row["shadow_exceedance_rate"] == 1


def test_change_frequency_and_retention_are_idempotent(tmp_path):
    source = vm()
    build_shadow_advice([source])
    archive_and_score(tmp_path, source)
    second = copy.deepcopy(source)
    second["shadow_comparison"]["candidate"]["action"] = "scale_out"
    result = archive_and_score(tmp_path, second, issued=T+1000)
    assert result["change_rows"][0]["comparable_transitions"] == 1
    assert result["change_rows"][0]["baseline_changes"] == 0
    assert result["change_rows"][0]["shadow_changes"] == 1
    with patch("time.time", return_value=(T+8*86400000)/1000):
        score_realized_forecasts(tmp_path)
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert db.execute("SELECT COUNT(*) FROM shadow_runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM shadow_budgets").fetchone()[0] == 0


def test_partial_rerun_does_not_reuse_old_pair():
    source = vm()
    build_shadow_advice([source])
    fresh = vm()
    fresh["charts_forecast"] = {"cpu": fresh["charts_forecast"]["cpu"]}
    build_shadow_advice([fresh])
    merged, = merge_partial_forecast_items([source], [fresh], metric_names_by_resource={"vm-a": {"cpu"}})
    assert merged["shadow_comparison"]["status"] == "unavailable"
