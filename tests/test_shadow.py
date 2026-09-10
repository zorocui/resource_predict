import copy
from unittest.mock import patch

import numpy as np
import pytest

from resource_predict.core.decision import build_scaling_advice
from resource_predict.pipeline.realized_error import _basis
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






def test_partial_rerun_does_not_reuse_old_pair():
    source = vm()
    build_shadow_advice([source])
    fresh = vm()
    fresh["charts_forecast"] = {"cpu": fresh["charts_forecast"]["cpu"]}
    build_shadow_advice([fresh])
    merged, = merge_partial_forecast_items([source], [fresh], metric_names_by_resource={"vm-a": {"cpu"}})
    assert merged["shadow_comparison"]["status"] == "unavailable"


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


@pytest.mark.parametrize("replicas", [2, 3])
def test_container_budgets_and_replica_counterfactual_limit(replicas):
    source = workload()
    candidate = copy.deepcopy(source["scaling_advice"])
    candidate["target_spec"]["replicas"] = replicas
    candidate["target_spec"]["containers"]["app"]["cpu_request_cores"] = 0.25
    with patch("resource_predict.pipeline.shadow.build_k8s_workload_advice", return_value=candidate):
        build_shadow_advice([source])
    request = next(row for row in source["shadow_comparison"]["budgets"] if row["metric"] == "cpu_request")
    assert request["baseline_allocation"] == 1
    assert request["shadow_allocation"] == 0.25 * replicas
    assert request["role"] == "request_budget"
    if replicas == 3:
        assert request["skip_reason"] == "replicas_changed"
