from __future__ import annotations

from typing import Any, Dict

import numpy as np

from resource_predict.resource_types import resource_type_of
from resource_predict.settings import settings


def _stats(values: np.ndarray) -> Dict[str, float]:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"avg": 0.0, "p95": 0.0, "peak": 0.0}
    return {
        "avg": float(np.mean(arr)),
        "p95": float(np.percentile(arr, 95)),
        "peak": float(np.max(arr)),
    }


def _quality_level(resource: Dict[str, Any], metric: str) -> str:
    q = resource.get("data_quality", {})
    if not isinstance(q, dict):
        return "unknown"
    block = q.get(metric, {})
    if not isinstance(block, dict):
        return "unknown"
    return str(block.get("level") or "unknown").lower()


def _has_denominator(spec: Dict[str, Any], metric: str) -> bool:
    if metric == "cpu":
        return bool(spec.get("cpu_request_cores") or spec.get("cpu_limit_cores"))
    if metric == "memory":
        return bool(spec.get("memory_limit_gb") or spec.get("memory_request_gb"))
    return False


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except Exception:
        return None
    return v if np.isfinite(v) and v > 0 else None


def _policy_tier(spec: Dict[str, Any]) -> str:
    cfg = settings.decision
    explicit = str(spec.get("policy_tier") or "").lower().strip()
    if explicit in {"conservative", "balanced", "aggressive"}:
        return explicit
    text = " ".join(
        str(spec.get(k) or "").lower()
        for k in ("namespace", "cluster", "owner_name", "workload_name", "pod")
    )
    if any(x and x in text for x in cfg.conservative_namespaces):
        return "conservative"
    if any(x and x in text for x in cfg.aggressive_namespaces):
        return "aggressive"
    default = str(cfg.default_policy_tier or "balanced").lower().strip()
    return default if default in {"conservative", "balanced", "aggressive"} else "balanced"


def _target_utilization(tier: str, action: str) -> float:
    if action == "scale_out_candidate":
        if tier == "conservative":
            return 0.65
        if tier == "aggressive":
            return 0.78
        return 0.72
    if tier == "conservative":
        return 0.55
    if tier == "aggressive":
        return 0.75
    return 0.68


def _recommend_k8s_policy(
    *,
    spec: Dict[str, Any],
    by_metric: Dict[str, Dict[str, float]],
    metric_actions: Dict[str, str],
    tier: str,
) -> Dict[str, Any]:
    policy: Dict[str, Any] = {"policy_tier": tier, "recommendations": {}, "notes": []}
    bases = {
        "cpu": _num(spec.get("cpu_request_cores")) or _num(spec.get("cpu_limit_cores")),
        "memory": _num(spec.get("memory_limit_gb")) or _num(spec.get("memory_request_gb")),
    }
    for metric, base in bases.items():
        action = metric_actions.get(metric, "hold")
        if base is None or action not in {"scale_out_candidate", "scale_in_candidate"}:
            continue
        st = by_metric.get(metric, {})
        load = max(float(st.get("p95", 0.0)), float(st.get("peak", 0.0)), 0.01)
        target_util = _target_utilization(tier, action)
        target = float(base) * load / target_util
        if action == "scale_in_candidate":
            floor_ratio = 0.5 if tier != "aggressive" else 0.35
            target = max(float(base) * floor_ratio, min(float(base), target))
        else:
            target = max(float(base), target)
        if metric == "cpu":
            request = round(max(0.05, target), 3)
            limit = round(max(request * 1.25, _num(spec.get("cpu_limit_cores")) or request), 3)
            policy["recommendations"]["cpu"] = {
                "request_cores": request,
                "limit_cores": limit,
                "target_utilization": target_util,
                "base_cores": base,
                "action": action,
            }
        else:
            request = round(max(0.064, target), 3)
            limit = round(max(request * 1.2, _num(spec.get("memory_limit_gb")) or request), 3)
            policy["recommendations"]["memory"] = {
                "request_gb": request,
                "limit_gb": limit,
                "target_utilization": target_util,
                "base_gb": base,
                "action": action,
            }
    if any(v == "scale_out_candidate" for v in metric_actions.values()):
        policy["notes"].append("consider HPA when CPU drives the recommendation")
    if any(v == "scale_in_candidate" for v in metric_actions.values()):
        policy["notes"].append("apply gradually and observe one cooldown window")
    policy["ready_for_execution"] = bool(policy["recommendations"])
    return policy


def _risk_profile(
    *,
    action: str,
    by_metric: Dict[str, Dict[str, float]],
    tier: str,
    blockers: list[str],
) -> Dict[str, Any]:
    high = max((float(st.get("p95", 0.0)) for st in by_metric.values()), default=0.0)
    low = min((float(st.get("avg", 0.0)) for st in by_metric.values()), default=0.0)
    idle = 100.0 * max(0.0, 0.3 - low) / 0.3
    saturation = min(100.0, 100.0 * max(0.0, high - 0.75) / 0.25)
    risk_score = 100.0 * high if action == "scale_out_candidate" else idle
    return {
        "policy_tier": tier,
        "risk_score": round(min(100.0, risk_score), 2),
        "saturation_risk": round(saturation, 2),
        "idle_opportunity": round(idle, 2),
        "blockers": blockers,
    }


def build_k8s_workload_advice(
    metric_future_values: Dict[str, np.ndarray],
    *,
    resource: Dict[str, Any],
) -> Dict[str, Any]:
    spec = resource.get("spec", {})
    if not isinstance(spec, dict):
        spec = {}
    tier = _policy_tier(spec)

    by_metric = {m: _stats(metric_future_values.get(m, np.array([]))) for m in ("cpu", "memory")}
    metric_actions: Dict[str, str] = {}
    metric_reasons: Dict[str, str] = {}
    blockers: list[str] = []
    baseline_missing: list[str] = []

    for metric in ("cpu", "memory"):
        st = by_metric[metric]
        label = "CPU" if metric == "cpu" else "memory"
        quality = _quality_level(resource, metric)
        has_base = _has_denominator(spec, metric)
        if quality == "poor":
            metric_actions[metric] = "insufficient_data"
            metric_reasons[metric] = f"{label} data quality is poor; skip execution recommendation"
            blockers.append(metric)
            continue
        if not has_base:
            baseline_missing.append(metric)
        if st["p95"] >= 0.8 or st["peak"] >= 0.9:
            metric_actions[metric] = "scale_out_candidate"
            metric_reasons[metric] = (
                f"{label} forecast is high(P95={st['p95'] * 100:.1f}%, peak={st['peak'] * 100:.1f}%)"
            )
        elif st["avg"] < 0.2 and st["p95"] < 0.35:
            metric_actions[metric] = "scale_in_candidate"
            metric_reasons[metric] = (
                f"{label} forecast is low(avg={st['avg'] * 100:.1f}%, P95={st['p95'] * 100:.1f}%)"
            )
        else:
            metric_actions[metric] = "hold"
            metric_reasons[metric] = f"{label} load is within the target range"
        if not has_base:
            metric_reasons[metric] += "; lacks request/limit baseline, trend only"

    actions = set(metric_actions.values())
    if "scale_out_candidate" in actions:
        action = "scale_out_candidate"
    elif actions == {"scale_in_candidate"} or ("scale_in_candidate" in actions and "hold" in actions):
        action = "scale_in_candidate"
    elif blockers and len(blockers) == len(metric_actions):
        action = "insufficient_data"
    else:
        action = "hold"

    confidence_score = 50.0
    quality_levels = [_quality_level(resource, m) for m in ("cpu", "memory")]
    if any(x == "poor" for x in quality_levels):
        confidence_score -= 18.0
    if any(x == "fair" for x in quality_levels):
        confidence_score -= 8.0
    if action in {"scale_out_candidate", "scale_in_candidate"}:
        confidence_score += 18.0
    if blockers:
        confidence_score -= 12.0

    target_policy = _recommend_k8s_policy(
        spec=spec,
        by_metric=by_metric,
        metric_actions=metric_actions,
        tier=tier,
    )
    if target_policy.get("ready_for_execution"):
        confidence_score += 4.0
    if baseline_missing and not target_policy.get("ready_for_execution"):
        confidence_score -= 6.0
    confidence_score = max(0.0, min(100.0, confidence_score))
    confidence = "high" if confidence_score >= 72 else "medium" if confidence_score >= 45 else "low"

    if action == "scale_out_candidate":
        required = max(1, int(settings.decision.scale_out_confirmations) - (1 if tier == "conservative" else 0))
    elif action == "scale_in_candidate":
        required = int(settings.decision.scale_in_confirmations) + (1 if tier == "conservative" else 0)
        if tier == "aggressive":
            required = max(1, required - 1)
    else:
        required = 1

    if baseline_missing:
        missing_names = ", ".join("CPU" if m == "cpu" else "memory" for m in baseline_missing)
        target_policy.setdefault("notes", []).append(
            f"{missing_names} lacks request/limit baseline; recommendation is trend-only"
        )
    reason = "; ".join(metric_reasons[m] for m in ("cpu", "memory") if m in metric_reasons)
    return {
        "resource_type": resource_type_of(resource),
        "action": action,
        "reason": reason,
        "confidence": confidence,
        "confidence_score": round(confidence_score, 2),
        "policy_tier": tier,
        "risk_profile": _risk_profile(action=action, by_metric=by_metric, tier=tier, blockers=blockers),
        "action_gate": {
            "state": "observe" if action != "hold" and required > 1 else "ready",
            "required_consistent_rounds": int(required),
            "observed_consistent_rounds": 1 if action != "hold" else 0,
            "reason": "needs repeated confirmation before execution"
            if action != "hold" and required > 1
            else "ready for execution review",
        },
        "metric_actions": metric_actions,
        "metric_reasons": metric_reasons,
        "stats": by_metric,
        "data_quality": resource.get("data_quality", {}),
        "target_k8s_policy": target_policy,
        "analysis_only": True,
    }
