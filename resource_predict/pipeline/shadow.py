"""Frozen, non-executable comparison of point-forecast and upper-bound advice."""
from __future__ import annotations

import copy
import logging
import math

import numpy as np

from resource_predict.core.decision import build_scaling_advice
from resource_predict.core.k8s_workload_decision import build_k8s_workload_advice
from resource_predict.pipeline.realized_error import _basis
from resource_predict.resource_types import metric_names_for_resource, resource_type_of

logger = logging.getLogger(__name__)


def _upper(item: dict, container: str, metric: str, chart: dict) -> np.ndarray:
    block = chart.get("calibration", {})
    values = block.get("upper", [])
    if (block.get("status") != "calibrated" or block.get("basis") != _basis(item, container, metric)
            or not values or len(values) != len(chart.get("x_pred_ms", []))):
        raise ValueError("incomplete_or_incomparable_calibration")
    if any(value is None or not math.isfinite(float(value)) or float(value) < 0 for value in values):
        raise ValueError("invalid_upper_bound")
    return np.asarray(values, dtype=float)


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _budgets(item, baseline, shadow):
    spec = item.get("spec", {})
    kind = resource_type_of(item)
    fields = ({"cpu": "cpu_cores", "memory": "memory_gb", "disk": "disk_gb"} if kind != "k8s_workload"
              else {"cpu_request": "cpu_request_cores", "cpu_limit": "cpu_limit_cores",
                    "memory_request": "memory_request_gb", "memory_limit": "memory_limit_gb"})
    containers = spec.get("containers", {}) if kind == "k8s_workload" else {"": spec}
    current_replicas = _positive(spec.get("replicas_observed")) if kind == "k8s_workload" else 1.0
    baseline_replicas = _positive(baseline.get("replicas", current_replicas))
    shadow_replicas = _positive(shadow.get("replicas", current_replicas))
    rows = []
    for container, current in containers.items():
        before = baseline.get("containers", {}).get(container, {}) if container else baseline
        after = shadow.get("containers", {}).get(container, {}) if container else shadow
        for metric, field in fields.items():
            current_value = _positive(current.get(field))
            base_value = _positive(before.get(field, current_value))
            shadow_value = _positive(after.get(field, current_value))
            reason = None
            if None in (current_value, base_value, shadow_value, current_replicas, baseline_replicas, shadow_replicas):
                reason = "missing_capacity"
            elif kind == "k8s_workload":
                mode = item.get("container_metric_modes", {}).get(container, {}).get(metric, "")
                if "/" not in mode:
                    reason = "non_ratio_metric"
                elif baseline_replicas != current_replicas or shadow_replicas != current_replicas:
                    reason = "replicas_changed"
            rows.append({"container": container, "metric": metric, "field": field,
                         "unit": "cores" if metric.startswith("cpu") else "GiB",
                         "role": "request_budget" if metric.endswith("request") else "capacity",
                         "baseline_allocation": base_value * baseline_replicas if base_value and baseline_replicas else None,
                         "shadow_allocation": shadow_value * shadow_replicas if shadow_value and shadow_replicas else None,
                         "baseline_ratio": base_value/current_value if reason is None else None,
                         "shadow_ratio": shadow_value/current_value if reason is None else None,
                         "skip_reason": reason})
    return rows


def build_shadow_advice(items: list[dict]) -> None:
    """Only call on fresh items, before partial merges. No task creation or execution."""
    for item in items:
        comparison = {"version": 1, "mode": "shadow", "executable": False,
                      "status": "unavailable", "reason": None,
                      "source_spec": copy.deepcopy(item.get("spec", {})),
                      "baseline_stage": "before_cross_run_confirmation",
                      "forecast_windows": {metric: chart.get("x_pred_ms", [])
                                           for metric, chart in item.get("charts_forecast", {}).items()}}
        item["shadow_comparison"] = comparison
        try:
            baseline = item.get("scaling_advice")
            if not isinstance(baseline, dict):
                raise ValueError("incomplete_fresh_prediction")
            candidate = calibrated_advice(item)
            # Explicitly exclude any execution gate/authorization from the shadow object.
            snapshot_fields = ("action", "target_spec", "policy_tier")
            comparison.update(status="paired", baseline={k: copy.deepcopy(baseline.get(k)) for k in snapshot_fields},
                              candidate={k: copy.deepcopy(candidate.get(k)) for k in snapshot_fields})
            comparison["budgets"] = _budgets(item, baseline.get("target_spec") or {}, candidate.get("target_spec") or {})
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            comparison.update(status="unavailable", reason=str(exc))
            logger.info("[shadow] %s: %s", item.get("resource_id"), exc)


def calibrated_advice(item: dict) -> dict:
    """Rebuild a complete recommendation using the same validated upper curves."""
    metrics = metric_names_for_resource(item)
    charts = item.get("charts_forecast", {})
    if any(metric not in charts for metric in metrics):
        raise ValueError("incomplete_fresh_prediction")
    futures = {metric: _upper(item, "", metric, charts[metric]) for metric in metrics}
    if resource_type_of(item) == "k8s_workload":
        containers = item.get("spec", {}).get("containers", {})
        if not containers:
            raise ValueError("missing_container_specs")
        container_futures = {}
        for container in containers:
            blocks = item.get("container_charts_forecast", {}).get(container, {})
            if any(metric not in blocks for metric in metrics):
                raise ValueError("incomplete_container_prediction")
            container_futures[container] = {
                metric: _upper(item, container, metric, blocks[metric]) for metric in metrics
            }
        candidate = build_k8s_workload_advice(
            futures, resource=copy.deepcopy(item), container_future_values=container_futures,
        )
    else:
        candidate = build_scaling_advice(futures, current_spec=copy.deepcopy(item.get("spec", {})),
                                         history_coverage=item.get("history_coverage"))
    return candidate
