from __future__ import annotations

import math
from typing import Any, Dict

from resource_predict.core.decision import policy_thresholds
from resource_predict.resource_types import resource_type_of
from resource_predict.utils import parse_float_or_none


def compute_urgency_score(item: Dict[str, Any], cfg: Any) -> float:
    """Return the bounded rule priority, independently of execution permission."""
    return float(compute_urgency_breakdown(item, cfg)["score"])


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _number(value: Any) -> float | None:
    parsed = parse_float_or_none(value)
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _saving_ratio(item: Dict[str, Any], advice: Dict[str, Any], *, k8s: bool) -> float:
    """Maximum dimension's reclaimable fraction; never add CPU and memory units."""
    spec = item.get("spec", {})
    target = advice.get("target_spec", {})
    if not isinstance(spec, dict) or not isinstance(target, dict):
        return 0.0
    ratios = []
    if not k8s:
        for field in ("cpu_cores", "memory_gb", "disk_gb"):
            current = _number(spec.get(field))
            proposed = _number(target.get(field))
            if current is not None and current > 0 and proposed is not None and proposed > 0:
                ratios.append(_unit(1.0 - proposed / current))
        return max(ratios, default=0.0)

    containers = spec.get("containers", {})
    targets = target.get("containers", {})
    if not isinstance(containers, dict) or not isinstance(targets, dict):
        return 0.0
    current_replicas = _number(spec.get("replicas") or spec.get("replicas_observed"))
    next_replicas = _number(target.get("replicas"))
    replica_ratio = 1.0
    if current_replicas is not None and current_replicas > 0 and next_replicas is not None and next_replicas > 0:
        replica_ratio = next_replicas / current_replicas
    for field in ("cpu_request_cores", "cpu_limit_cores", "memory_request_gb", "memory_limit_gb"):
        current_total = target_total = 0.0
        complete = bool(containers)
        for name, container in containers.items():
            proposed = targets.get(name, {})
            if not isinstance(container, dict) or not isinstance(proposed, dict):
                complete = False
                break
            current = _number(container.get(field))
            following = _number(proposed.get(field, container.get(field)))
            if current is None or current <= 0 or following is None or following <= 0:
                complete = False
                break
            current_total += current
            target_total += following
        if complete and current_total > 0:
            ratios.append(_unit(1.0 - target_total * replica_ratio / current_total))
    return max(ratios, default=0.0)


def compute_urgency_breakdown(item: Dict[str, Any], cfg: Any) -> Dict[str, Any]:
    """Version 2: explainable capacity-risk or savings rule score, not a probability.

    Per-container evidence avoids masking hot containers with Workload averages.
    """
    result: Dict[str, Any] = {
        "version": 2, "score_max": 100, "score": 0.0, "level": "unknown",
        "kind": "unknown", "components": [], "metric_scores": [],
    }
    advice = item.get("scaling_advice", {}) if isinstance(item, dict) else {}
    if not isinstance(advice, dict):
        return result
    action = str(advice.get("action", "")).lower()
    if action == "hold":
        result.update(level="none", kind="none", components=[{"label": "保持动作", "value": 0.0}])
        return result
    expanding = action in {"scale_out", "scale_out_candidate"}
    if not expanding and action not in {"scale_in", "scale_in_candidate"}:
        return result
    k8s = resource_type_of(item) == "k8s_workload"
    thresholds = policy_thresholds(str(advice.get("policy_tier", "balanced")), cfg)
    metrics = ("cpu", "memory") if k8s else ("cpu", "memory", "disk")
    direction = {"scale_out", "scale_out_candidate"} if expanding else {"scale_in", "scale_in_candidate"}
    sources = advice.get("container_advice") if k8s else None
    if not isinstance(sources, dict) or not sources:
        sources = {"": advice}
    saving = _saving_ratio(item, advice, k8s=k8s) if not expanding else 0.0
    best_components = []
    best_score = -1.0
    for container, source in sources.items():
        if not isinstance(source, dict):
            continue
        stats = source.get("stats", {})
        actions = source.get("metric_actions", {})
        if not isinstance(stats, dict) or not isinstance(actions, dict):
            continue
        for metric in metrics:
            metric_action = str(actions.get(metric, action)).lower()
            st = stats.get(metric)
            if metric_action not in direction or not isinstance(st, dict):
                continue
            if "sample_count" in st and (_number(st["sample_count"]) or 0) <= 0:
                continue
            avg, p95, peak = (_number(st.get(key)) for key in ("avg", "p95", "peak"))
            if any(value is None or value < 0 for value in (avg, p95, peak)):
                continue
            if expanding:
                out_threshold = thresholds["disk_scale_out_threshold" if metric == "disk" else "scale_out_threshold"]
                peak_threshold = thresholds["disk_peak_guard_threshold" if metric == "disk" else "peak_guard_threshold"]
                p95_pressure = _unit((p95 - out_threshold) / max(1.0 - out_threshold, 0.001))
                peak_pressure = _unit((peak - peak_threshold) / max(1.0 - peak_threshold, 0.001))
                breached = p95 >= out_threshold or peak >= peak_threshold
                components = [
                    {"label": "触及容量阈值", "value": 40.0 if breached else 0.0},
                    {"label": "超阈值压力（P95与折减峰值取最大）", "value": 60.0 * max(p95_pressure, 0.75 * peak_pressure)},
                ]
            else:
                headroom = min(
                    _unit((thresholds["scale_in_threshold"] - avg) / max(thresholds["scale_in_threshold"], 0.001)),
                    _unit((thresholds["scale_in_p95_guard"] - p95) / max(thresholds["scale_in_p95_guard"], 0.001)),
                )
                low_ratio = _unit(_number(st.get("low_ratio")) or 0.0)
                components = [
                    {"label": "保守空闲程度", "value": 60.0 * headroom},
                    {"label": "持续低负载比例", "value": 25.0 * low_ratio},
                    {"label": "目标总容量回收比例", "value": 15.0 * saving},
                ]
            score = round(sum(part["value"] for part in components), 3)
            result["metric_scores"].append({
                "metric": metric, "container": container or None, "action": metric_action, "value": score,
            })
            if score > best_score:
                best_score = score
                best_components = components
    if best_score < 0:
        return result
    result.update(
        score=best_score,
        kind="capacity_risk" if expanding else "savings",
        level="critical" if best_score >= 90 else "high" if best_score >= 70 else "medium" if best_score >= 40 else "low",
        components=[{"label": part["label"], "value": round(part["value"], 3)} for part in best_components],
    )
    return result
