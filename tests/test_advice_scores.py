import copy

import numpy as np
import pytest

from resource_predict.core.decision import aggregate_confidence
from resource_predict.core.k8s_workload_decision import _merge_container_advice, build_k8s_workload_advice
from resource_predict.services.urgency import compute_urgency_breakdown
from resource_predict.settings import settings


def test_confidence_opposite_action_cannot_lend_its_score():
    actions, reasons, scores = {}, {}, {}
    _merge_container_advice(actions, reasons, scores, {
        "hot": {"metric_actions": {"cpu": "scale_out_candidate"}, "confidence_metric_scores": {"cpu": 10}},
        "idle": {"metric_actions": {"cpu": "scale_in_candidate"}, "confidence_metric_scores": {"cpu": 95}},
    })
    assert actions == {"cpu": "scale_out_candidate"}
    assert scores == {"cpu": 10}


def test_confidence_shrink_uses_weakest_container_and_metric():
    actions, reasons, scores = {}, {}, {}
    _merge_container_advice(actions, reasons, scores, {
        name: {"metric_actions": {"cpu": "scale_in_candidate"}, "confidence_metric_scores": {"cpu": value}}
        for name, value in (("a", 95), ("b", 10))
    })
    assert scores["cpu"] == 10
    result = aggregate_confidence({"cpu": "scale_in", "memory": "scale_in"}, {"cpu": 100, "memory": 0}, has_mixed_signals=False)
    assert result["score"] == 0


def test_mixed_confidence_cannot_cross_execution_threshold():
    result = aggregate_confidence({"cpu": "scale_out", "memory": "scale_in"}, {"cpu": 100, "memory": 100}, has_mixed_signals=True)
    assert result["score"] == 71
    assert sum(p["value"] for p in result["breakdown"]["components"]) == 71


def _resource():
    return {
        "resource_type": "k8s_workload",
        "history_coverage": {"span_hours": 240},
        "spec": {"namespace": "default", "workload_kind": "Deployment", "replicas": 1, "containers": {
            "a": {"cpu_request_cores": 1, "cpu_limit_cores": 2, "memory_request_gb": 1, "memory_limit_gb": 2},
            "b": {"cpu_request_cores": 1, "cpu_limit_cores": 2, "memory_request_gb": 1, "memory_limit_gb": 2},
        }},
        "data_quality": {key: {"level": "good"} for key in ("cpu_limit", "cpu_request", "memory_limit", "memory_request")},
    }


def _futures(value):
    return {key: np.full(48, value) for key in ("cpu_limit", "cpu_request", "memory_limit", "memory_request")}


@pytest.mark.parametrize("container_only", [False, True])
def test_k8s_quality_penalty_survives_container_merge(container_only):
    resource = _resource()
    futures = _futures(.99)
    good = build_k8s_workload_advice(futures, resource=resource, container_future_values={"a": futures, "b": futures})
    fair = copy.deepcopy(resource["data_quality"])
    fair["cpu_limit"]["level"] = "fair"
    if container_only:
        resource["container_data_quality"] = {"a": fair}
    else:
        resource["data_quality"] = fair
    adjusted = build_k8s_workload_advice(futures, resource=resource, container_future_values={"a": futures, "b": futures})
    assert adjusted["confidence_score"] == pytest.approx(good["confidence_score"] - 8)
    assert sum(p["value"] for p in adjusted["confidence_breakdown"]["components"]) == pytest.approx(adjusted["confidence_score"])


def test_k8s_opposite_containers_remain_mixed_and_capped():
    advice = build_k8s_workload_advice(_futures(.5), resource=_resource(), container_future_values={"a": _futures(.99), "b": _futures(.01)})
    assert advice["action"] == "scale_out_candidate"
    assert advice["has_mixed_signals"]
    assert advice["confidence_score"] <= 71
    assert advice["container_advice"]["a"]["stats"]["cpu"]["p95"] == pytest.approx(.99)


def _item(value=1.0):
    return {"resource_type": "openstack_vm", "scaling_advice": {
        "action": "scale_out", "metric_actions": {"cpu": "scale_out"},
        "stats": {"cpu": {"avg": value, "p95": value, "peak": value}},
    }}


def test_urgency_bounded_monotone_and_no_missing_metric_bonus():
    values = [0, .8, .9, .99, 1, 2, 1000]
    results = [compute_urgency_breakdown(_item(value), settings.decision) for value in values]
    scores = [r["score"] for r in results]
    assert scores == sorted(scores)
    assert scores[-1] == 100
    assert all(0 <= score <= 100 for score in scores)
    assert len(results[-1]["metric_scores"]) == 1
    assert results[-1]["level"] == "critical"
    assert results[-1]["kind"] == "capacity_risk"


def test_urgency_ignores_confidence_readiness_and_duplicate_risk():
    item = _item(.95)
    baseline = compute_urgency_breakdown(item, settings.decision)
    item["scaling_advice"].update(confidence="high", risk_profile={"risk_score": 100}, analysis_only=True, has_mixed_signals=True)
    assert compute_urgency_breakdown(item, settings.decision) == baseline


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1])
def test_urgency_missing_or_invalid_data_is_not_idle(bad):
    item = _item()
    item["scaling_advice"]["action"] = "scale_in"
    item["scaling_advice"]["metric_actions"]["cpu"] = "scale_in"
    item["scaling_advice"]["stats"]["cpu"]["avg"] = bad
    result = compute_urgency_breakdown(item, settings.decision)
    assert result["level"] == "unknown"
    assert result["score"] == 0


def test_container_total_saving_accounts_for_replicas_without_mutating_spec():
    item = _resource()
    item["scaling_advice"] = {
        "action": "scale_in_candidate", "metric_actions": {"cpu": "scale_in_candidate"},
        "stats": {"cpu": {"avg": .01, "p95": .01, "peak": .01, "low_ratio": 1}},
        "target_spec": {"containers": {"a": {"cpu_request_cores": .5}, "b": {"cpu_request_cores": .5}}},
    }
    before = copy.deepcopy(item)
    result = compute_urgency_breakdown(item, settings.decision)
    assert result["components"][-1]["value"] == 7.5
    assert item == before
    item["scaling_advice"]["target_spec"]["replicas"] = 2
    assert compute_urgency_breakdown(item, settings.decision)["components"][-1]["value"] == 0


def test_hot_container_is_not_hidden_by_workload_mean():
    item = _resource()
    item["scaling_advice"] = {
        "action": "scale_out_candidate", "stats": {"cpu": {"avg": .5, "p95": .5, "peak": .5}},
        "container_advice": {"a": _item(1)["scaling_advice"]},
    }
    result = compute_urgency_breakdown(item, settings.decision)
    assert result["score"] == 100
    assert result["metric_scores"][0]["container"] == "a"


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_empty_k8s_forecast_is_not_a_high_confidence_shrink(value):
    resource = _resource()
    futures = {} if value is None else _futures(value)
    advice = build_k8s_workload_advice(futures, resource=resource, container_future_values={"a": futures, "b": futures})
    assert advice["action"] == "insufficient_data"
    assert advice["confidence_score"] == 0
    resource["scaling_advice"] = advice
    assert compute_urgency_breakdown(resource, settings.decision)["level"] == "unknown"


def test_missing_container_baseline_survives_merge():
    resource = _resource()
    futures = _futures(.99)
    good = build_k8s_workload_advice(futures, resource=resource, container_future_values={"a": futures, "b": futures})
    del resource["spec"]["containers"]["b"]["memory_request_gb"]
    del resource["spec"]["containers"]["b"]["memory_limit_gb"]
    missing = build_k8s_workload_advice(futures, resource=resource, container_future_values={"a": futures, "b": futures})
    assert missing["confidence_score"] == pytest.approx(good["confidence_score"] - 6)
