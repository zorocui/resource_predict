from __future__ import annotations

from typing import Any, Dict

import numpy as np

from resource_predict.core.decision import policy_thresholds
from resource_predict.resource_types import resource_type_of
from resource_predict.settings import settings
from resource_predict.utils import (
    compute_metric_stats,
    parse_positive_finite,
    parse_positive_int,
    resolve_policy_tier,
)

_K8S_TIER_FIELDS = ("namespace", "cluster", "owner_name", "workload_name", "pod")


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
        return bool(spec.get("cpu_limit_cores") or spec.get("cpu_request_cores"))
    if metric == "memory":
        return bool(spec.get("memory_limit_gb") or spec.get("memory_request_gb"))
    return False


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


def _workload_kind(spec: Dict[str, Any]) -> str:
    raw = spec.get("workload_kind") or spec.get("owner_kind") or ""
    return str(raw).strip().lower().replace("-", "")


def _supports_replica_scaling(spec: Dict[str, Any]) -> bool:
    return _workload_kind(spec) in {"deployment", "statefulset", "replicaset"}


def _recommend_k8s_policy(
    *,
    spec: Dict[str, Any],
    by_metric: Dict[str, Dict[str, float]],
    metric_actions: Dict[str, str],
    tier: str,
) -> Dict[str, Any]:
    policy: Dict[str, Any] = {"policy_tier": tier, "recommendations": {}, "notes": []}
    bases = {
        "cpu": parse_positive_finite(spec.get("cpu_limit_cores")) or parse_positive_finite(spec.get("cpu_request_cores")),
        "memory": parse_positive_finite(spec.get("memory_limit_gb")) or parse_positive_finite(spec.get("memory_request_gb")),
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
            # Scale-out: per-replica target stays at base. Replicas already
            # absorb the headroom in _recommend_replicas, so increasing
            # per-replica resources here would cause double-scaling
            # (per-replica increase * replica increase = multiplicative
            # over-provisioning).
            target = float(base)
        if metric == "cpu":
            request = _round_k8s_even_target(target, action=action, base=base)
            if request is None:
                continue
            limit_base = parse_positive_finite(spec.get("cpu_limit_cores"))
            limit = _round_k8s_even_target_limit(
                request * 1.25,
                action=action,
                current_limit=limit_base,
                request=request,
            )
            policy["recommendations"]["cpu"] = {
                "request_cores": request,
                "limit_cores": limit,
                "target_utilization": target_util,
                "base_cores": base,
                "action": action,
            }
        else:
            request = _round_k8s_even_target(target, action=action, base=base)
            if request is None:
                continue
            limit_base = parse_positive_finite(spec.get("memory_limit_gb"))
            limit = _round_k8s_even_target_limit(
                request * 1.2,
                action=action,
                current_limit=limit_base,
                request=request,
            )
            policy["recommendations"]["memory"] = {
                "request_gb": request,
                "limit_gb": limit,
                "target_utilization": target_util,
                "base_gb": base,
                "action": action,
            }
    replica_target = _recommend_replicas(spec=spec, by_metric=by_metric, metric_actions=metric_actions, tier=tier)
    if replica_target:
        policy["recommendations"]["replicas"] = replica_target
    if any(v == "scale_out_candidate" for v in metric_actions.values()):
        policy["notes"].append("consider HPA when CPU drives the recommendation")
    if any(v == "scale_in_candidate" for v in metric_actions.values()):
        policy["notes"].append("apply gradually and observe one cooldown window")
    if _workload_kind(spec) == "daemonset":
        policy["notes"].append("DaemonSet replicas follow node scheduling; only requests/limits are adjustable here")
    # Track metrics with scale signals but missing baseline data.
    # These cannot produce per-replica recommendations, so the policy
    # is not fully executable even if other metrics have recommendations.
    needed_data_metrics = {
        m for m, a in metric_actions.items()
        if a in {"scale_out_candidate", "scale_in_candidate"}
        and m not in policy["recommendations"]
        and not _has_denominator(spec, m)
    }
    policy["ready_for_execution"] = bool(policy["recommendations"]) and not needed_data_metrics
    return policy


def _round_small_k8s_target(value: float) -> float:
    """保留小规格 Workload 的毫核/Mi 粒度，避免被偶数整数规则放大。"""
    return max(0.001, round(float(value), 3))


def _round_k8s_even_target(value: float, *, action: str, base: float) -> int | float | None:
    """对齐 K8S 目标规格。

    大规格仍按偶数整数对齐；小于 2C/2Gi 的 Workload 保留小数粒度，
    避免 0.5C 级别的 request/limit 被直接放大到 2C。
    """
    if action == "scale_in_candidate":
        if base < 2.0:
            # 小规格 workload 缩容时向上取到 2 会超过原始 base，跳过 per-replica 推荐
            return None
        rounded = int(np.floor(float(value) / 2.0) * 2)
        rounded = max(2, rounded)
        if rounded >= float(base):
            return None
        return rounded
    if base < 2.0 or float(value) < 2.0:
        return _round_small_k8s_target(value)
    rounded = int(np.ceil(float(value) / 2.0) * 2)
    return max(2, rounded)


def _round_k8s_even_target_limit(
    value: float,
    *,
    action: str,
    current_limit: float | None,
    request: int | float,
) -> int | float:
    largest_small_value = max(
        float(value),
        float(request),
        float(current_limit) if current_limit is not None else 0.0,
    )
    if largest_small_value < 2.0:
        target = max(float(value), float(request))
        if action == "scale_out_candidate" and current_limit is not None:
            target = max(target, float(current_limit))
        return _round_small_k8s_target(target)
    rounded = max(request, int(np.ceil(float(value) / 2.0) * 2), 2)
    if action == "scale_out_candidate" and current_limit is not None:
        rounded = max(rounded, int(np.ceil(float(current_limit) / 2.0) * 2))
    return rounded


def _recommend_replicas(
    *,
    spec: Dict[str, Any],
    by_metric: Dict[str, Dict[str, float]],
    metric_actions: Dict[str, str],
    tier: str,
) -> Dict[str, Any] | None:
    if not _supports_replica_scaling(spec):
        return None
    current = parse_positive_int(spec.get("replicas") or spec.get("current_replicas") or spec.get("replicas_observed"))
    if current is None:
        return None
    actions = set(metric_actions.values())
    if "scale_out_candidate" in actions:
        target_util = _target_utilization(tier, "scale_out_candidate")
        pressure = max(
            (
                max(float(st.get("p95", 0.0)), float(st.get("peak", 0.0)))
                for metric, st in by_metric.items()
                if metric_actions.get(metric) == "scale_out_candidate"
            ),
            default=0.0,
        )
        target = int(np.ceil(current * max(pressure, target_util) / target_util))
        target = max(current + 1, target)
        return {
            "current_replicas": current,
            "target_replicas": target,
            "target_utilization": target_util,
            "action": "scale_out_candidate",
        }
    if "scale_in_candidate" not in actions:
        return None
    target_util = _target_utilization(tier, "scale_in_candidate")
    pressure = max(
        (
            max(float(st.get("p95", 0.0)), float(st.get("avg", 0.0)))
            for metric, st in by_metric.items()
            if metric_actions.get(metric) == "scale_in_candidate"
        ),
        default=0.0,
    )
    target = int(np.floor(current * max(pressure, 0.05) / target_util))
    target = max(1, min(current - 1, target)) if current > 1 else 1
    if target >= current:
        return None
    return {
        "current_replicas": current,
        "target_replicas": target,
        "target_utilization": target_util,
        "action": "scale_in_candidate",
    }


def _target_spec_from_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    recs = policy.get("recommendations", {})
    if not isinstance(recs, dict):
        return {}
    target: Dict[str, Any] = {}
    cpu = recs.get("cpu")
    if isinstance(cpu, dict):
        if cpu.get("request_cores") is not None:
            target["cpu_request_cores"] = cpu["request_cores"]
            target["cpu_cores"] = cpu["request_cores"]
        if cpu.get("limit_cores") is not None:
            target["cpu_limit_cores"] = cpu["limit_cores"]
    memory = recs.get("memory")
    if isinstance(memory, dict):
        if memory.get("request_gb") is not None:
            target["memory_request_gb"] = memory["request_gb"]
            target["memory_gb"] = memory["request_gb"]
        if memory.get("limit_gb") is not None:
            target["memory_limit_gb"] = memory["limit_gb"]
    replicas = recs.get("replicas")
    if isinstance(replicas, dict) and replicas.get("target_replicas") is not None:
        target["replicas"] = replicas["target_replicas"]
    return target


def _trend_features(values: np.ndarray, window: int = 6) -> Dict[str, float]:
    """计算趋势特征：线性斜率和窗口均值变化量。"""
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = int(arr.size)
    if n < 2:
        return {"slope": 0.0, "window_mean_delta": 0.0}
    x = np.arange(n, dtype=float)
    slope = float(np.polyfit(x, arr, 1)[0])
    w = max(2, min(int(window), n // 2 if n >= 4 else n))
    if n >= 2 * w:
        first_mean = float(np.mean(arr[:w]))
        last_mean = float(np.mean(arr[-w:]))
        delta = last_mean - first_mean
    else:
        delta = float(arr[-1] - arr[0])
    return {"slope": slope, "window_mean_delta": delta}


def _max_consecutive(values: np.ndarray, predicate) -> int:
    """计算满足 predicate 条件的最长连续点数。"""
    best = 0
    cur = 0
    for v in values:
        if predicate(float(v)):
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def _bounded_above(value: float, threshold: float, *, soft_cap: float = 1.0) -> float:
    """0..1+ score for exceeding a threshold."""
    threshold = max(float(threshold), 0.001)
    value = float(value)
    if value <= threshold:
        return 0.0
    if value <= soft_cap:
        return min(1.0, (value - threshold) / max(soft_cap - threshold, 0.001))
    return 1.0 + min(1.0, float(np.log1p(value - soft_cap)))


def _bounded_below(value: float, threshold: float) -> float:
    """0..1 score for being safely below a threshold."""
    threshold = max(float(threshold), 0.001)
    return max(0.0, min(1.0, (threshold - float(value)) / threshold))


def _trend_support_score(st: Dict[str, float], *, direction: str) -> float:
    """评估趋势对某方向（up/down）的支持程度，返回 0..1 分数。"""
    cfg = settings.decision
    slope = float(st.get("slope", 0.0))
    delta = float(st.get("window_mean_delta", 0.0))
    if direction == "up":
        slope_part = min(1.0, max(0.0, slope) / max(float(cfg.uptrend_slope_threshold), 0.0001))
        delta_part = min(1.0, max(0.0, delta) / max(float(cfg.window_mean_delta_threshold), 0.0001))
    else:
        slope_part = min(1.0, max(0.0, -slope) / max(abs(float(cfg.downtrend_slope_threshold)), 0.0001))
        delta_part = min(1.0, max(0.0, -delta) / max(float(cfg.window_mean_delta_threshold), 0.0001))
    return 0.5 * slope_part + 0.5 * delta_part


def _metric_confidence_k8s(metric_action: str, st: Dict[str, float], th: Dict[str, float]) -> float:
    """Per-metric confidence score for K8S workload scaling signals."""
    avg = float(st.get("avg", 0.0))
    p95 = float(st.get("p95", 0.0))
    peak = float(st.get("peak", 0.0))
    gap = float(st.get("gap", 0.0))
    high_ratio = float(st.get("high_ratio", 0.0))
    low_ratio = float(st.get("low_ratio", 0.0))

    if metric_action == "scale_out_candidate":
        p95_strength = _bounded_above(p95, th["scale_out_threshold"])
        peak_strength = _bounded_above(peak, th["peak_guard_threshold"])
        avg_strength = _bounded_above(avg, th["scale_out_threshold"])
        persistence = max(high_ratio, min(1.0, p95_strength))
        trend = _trend_support_score(st, direction="up")
        spike_penalty = 0.0
        if peak_strength > 0.0 and p95_strength < 0.15:
            spike_penalty = min(18.0, 18.0 * min(1.0, gap / max(0.3, 0.001)))
        score = (
            42.0 * min(1.0, p95_strength)
            + 20.0 * min(1.0, peak_strength)
            + 14.0 * min(1.0, avg_strength)
            + 16.0 * persistence
            + 8.0 * trend
            - spike_penalty
        )
        return max(0.0, min(100.0, score))

    if metric_action == "scale_in_candidate":
        avg_headroom = _bounded_below(avg, th["scale_in_threshold"])
        p95_headroom = _bounded_below(p95, th["scale_in_p95_guard"])
        trend = _trend_support_score(st, direction="down")
        stability = 1.0 - min(1.0, gap / max(th["scale_in_p95_guard"], 0.001))
        uptrend_penalty = 12.0 * _trend_support_score(st, direction="up")
        score = (
            34.0 * avg_headroom
            + 30.0 * p95_headroom
            + 18.0 * low_ratio
            + 10.0 * trend
            + 8.0 * max(0.0, stability)
            - uptrend_penalty
        )
        return max(0.0, min(100.0, score))

    return 50.0


def _risk_profile(
    *,
    action: str,
    by_metric: Dict[str, Dict[str, float]],
    tier: str,
    blockers: list[str],
    th: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    # 仅基于有 baseline 的指标计算风险，原始使用量（cores/GB）不应参与利用率阈值比较
    metrics_with_baseline = {
        metric: st for metric, st in by_metric.items()
        if metric not in blockers  # blockers 包含没有 baseline 的指标
    }
    high = max((float(st.get("p95", 0.0)) for st in metrics_with_baseline.values()), default=0.0)
    low = min((float(st.get("avg", 0.0)) for st in metrics_with_baseline.values()), default=0.0)
    out_th = float(th["scale_out_threshold"]) if th else 0.8
    in_th = float(th["scale_in_threshold"]) if th else 0.2
    saturation = min(100.0, 100.0 * max(0.0, high - out_th) / max(1.0 - out_th, 0.01))
    idle = min(100.0, 100.0 * max(0.0, in_th - low) / max(in_th, 0.01))
    # 加入 high_ratio/low_ratio 权重，区分瞬态 spike 与持续高负载
    high_ratio = max(
        (float(st.get("high_ratio", 0.0)) for st in metrics_with_baseline.values()),
        default=0.0,
    )
    low_ratio = max(
        (float(st.get("low_ratio", 0.0)) for st in metrics_with_baseline.values()),
        default=0.0,
    )
    saturation = saturation * 0.65 + min(100.0, 100.0 * high_ratio) * 0.35
    idle = idle * 0.55 + min(100.0, 100.0 * low_ratio) * 0.45
    if action == "scale_out_candidate":
        risk_score = saturation
    elif action == "scale_in_candidate":
        risk_score = idle
    else:
        risk_score = max(saturation * 0.35, idle * 0.2)
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
    tier = resolve_policy_tier(spec, fields=_K8S_TIER_FIELDS)
    th = policy_thresholds(tier)
    cfg = settings.decision

    # Compute extended stats + trend + streak for each metric
    by_metric: Dict[str, Dict[str, float]] = {}
    for m in ("cpu", "memory"):
        vals = np.asarray(metric_future_values.get(m, np.array([])), dtype=float)
        st = compute_metric_stats(vals, extended=True)
        tr = _trend_features(vals, cfg.trend_window_points)
        st.update(tr)
        high_streak = _max_consecutive(vals, lambda x: x >= float(cfg.consecutive_high_threshold))
        low_streak = _max_consecutive(vals, lambda x: x <= float(cfg.consecutive_low_threshold))
        if vals.size > 0:
            st["high_ratio"] = float(np.mean(vals >= float(cfg.consecutive_high_threshold)))
            st["low_ratio"] = float(np.mean(vals <= float(cfg.consecutive_low_threshold)))
        else:
            st["high_ratio"] = 0.0
            st["low_ratio"] = 0.0
        st["high_streak"] = float(high_streak)
        st["low_streak"] = float(low_streak)
        by_metric[m] = st

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
            metric_actions[metric] = "hold"
            # 即使缺少基线，也提供趋势信息辅助运维判断
            slope = float(st.get("slope", 0.0))
            delta = float(st.get("window_mean_delta", 0.0))
            if abs(slope) >= float(cfg.uptrend_slope_threshold) or abs(delta) >= float(cfg.window_mean_delta_threshold):
                direction = "rising" if (slope > 0 or delta > 0) else "falling"
                trend_info = f"; trend is {direction}(slope={slope:.4f}, delta={delta:.4f})"
            else:
                trend_info = "; trend is stable"
            metric_reasons[metric] = (
                f"{label} lacks request/limit baseline; cannot compute utilization for recommendation"
                f"{trend_info}"
            )
            continue
        # Tier-aware utilization thresholds (shared with VM decision module)
        if st["p95"] >= th["scale_out_threshold"] or st["peak"] >= th["peak_guard_threshold"]:
            metric_actions[metric] = "scale_out_candidate"
            metric_reasons[metric] = (
                f"{label} forecast is high(P95={st['p95'] * 100:.1f}%, peak={st['peak'] * 100:.1f}%)"
            )
        elif st["avg"] < th["scale_in_threshold"] and st["p95"] < th["scale_in_p95_guard"]:
            # 趋势保护：当前偏低但趋势回升时，暂缓缩容
            has_strong_uptrend = (
                float(st.get("slope", 0.0)) >= float(cfg.uptrend_slope_threshold)
                and float(st.get("window_mean_delta", 0.0)) >= float(cfg.window_mean_delta_threshold)
            )
            if has_strong_uptrend:
                metric_actions[metric] = "hold"
                metric_reasons[metric] = (
                    f"{label} forecast is low(avg={st['avg'] * 100:.1f}%, P95={st['p95'] * 100:.1f}%), "
                    "but trend is rising; deferring scale-in"
                )
            else:
                metric_actions[metric] = "scale_in_candidate"
                metric_reasons[metric] = (
                    f"{label} forecast is low(avg={st['avg'] * 100:.1f}%, P95={st['p95'] * 100:.1f}%)"
                )
        else:
            metric_actions[metric] = "hold"
            metric_reasons[metric] = f"{label} load is within the target range"

    actions = set(metric_actions.values())
    if "scale_out_candidate" in actions:
        action = "scale_out_candidate"
    elif actions == {"scale_in_candidate"} or ("scale_in_candidate" in actions and "hold" in actions):
        action = "scale_in_candidate"
    elif blockers and len(blockers) == len(metric_actions):
        action = "insufficient_data"
    else:
        action = "hold"

    # Per-metric confidence scoring (replaces boolean加减)
    metric_confidence_scores: Dict[str, float] = {}
    for m in ("cpu", "memory"):
        m_action = metric_actions.get(m, "hold")
        if m_action in {"scale_out_candidate", "scale_in_candidate"}:
            metric_confidence_scores[m] = _metric_confidence_k8s(m_action, by_metric[m], th)

    if metric_confidence_scores:
        scores = list(metric_confidence_scores.values())
        confidence_score = 0.65 * max(scores) + 0.35 * float(np.mean(scores))
        if len(scores) >= 2:
            confidence_score += 4.0
    else:
        confidence_score = 50.0

    # Quality adjustments
    quality_levels = [_quality_level(resource, m) for m in ("cpu", "memory")]
    if any(x == "poor" for x in quality_levels):
        confidence_score -= 18.0
    if any(x == "fair" for x in quality_levels):
        confidence_score -= 8.0
    if blockers:
        confidence_score -= 12.0

    target_policy = _recommend_k8s_policy(
        spec=spec,
        by_metric=by_metric,
        metric_actions=metric_actions,
        tier=tier,
    )
    target_spec = _target_spec_from_policy(target_policy)

    # Total capacity coordination: 校验 per_replica * replicas 的总量合理性
    _coordinate_total_capacity(target_policy, target_spec, spec, by_metric, metric_actions)

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
        "confidence_metric_scores": {m: round(s, 2) for m, s in metric_confidence_scores.items()},
        "policy_tier": tier,
        "risk_profile": _risk_profile(
            action=action,
            by_metric=by_metric,
            tier=tier,
            blockers=blockers + baseline_missing,
            th=th,
        ),
        "action_gate": {
            "state": "observe" if action != "hold" and required > 1 else "ready",
            "required_consistent_rounds": int(required),
            "observed_consistent_rounds": 1 if (action != "hold" and required <= 1) else 0,
            "reason": "needs repeated confirmation before execution"
            if action != "hold" and required > 1
            else "ready for execution review",
        },
        "metric_actions": metric_actions,
        "metric_reasons": metric_reasons,
        "stats": by_metric,
        "data_quality": resource.get("data_quality", {}),
        "target_spec": target_spec,
        "target_k8s_policy": target_policy,
        "analysis_only": not bool(target_spec),
    }


def _coordinate_total_capacity(
    policy: Dict[str, Any],
    target_spec: Dict[str, Any],
    spec: Dict[str, Any],
    by_metric: Dict[str, Dict[str, float]],
    metric_actions: Dict[str, str],
) -> None:
    """校验并标注 per-replica × replicas 的总容量建议是否协调。

    当 per-replica 和 replicas 同时有推荐时，在 policy notes 中标注总量变化比例，
    帮助运维人员判断整体资源变动是否合理。
    """
    replica_rec = policy.get("recommendations", {}).get("replicas")
    if not isinstance(replica_rec, dict):
        return
    target_replicas = replica_rec.get("target_replicas")
    current_replicas = replica_rec.get("current_replicas")
    if not target_replicas or not current_replicas or target_replicas == current_replicas:
        return

    for metric, key_suffix in [("cpu", "request_cores"), ("memory", "request_gb")]:
        rec = policy.get("recommendations", {}).get(metric)
        if not isinstance(rec, dict):
            continue
        target_req = rec.get(key_suffix)
        base = rec.get(f"base_{key_suffix.split('_')[0]}{'_' + '_'.join(key_suffix.split('_')[1:])}" if "_" in key_suffix else f"base_{key_suffix}")
        # Try simpler key patterns: base_cores for cpu, base_gb for memory
        if base is None:
            base = rec.get("base_cores") if metric == "cpu" else rec.get("base_gb")
        if target_req is None or base is None:
            continue
        cur_total = float(base) * float(current_replicas)
        new_total = float(target_req) * float(target_replicas)
        if cur_total <= 0:
            continue
        change_ratio = new_total / cur_total - 1.0
        action = metric_actions.get(metric, "hold")
        label = "CPU" if metric == "cpu" else "memory"
        if action == "scale_in_candidate" and change_ratio < -0.6:
            policy.setdefault("notes", []).append(
                f"{label} total capacity reduction is {abs(change_ratio) * 100:.0f}%; "
                "consider applying in stages"
            )
        elif action == "scale_out_candidate" and change_ratio > 1.0:
            policy.setdefault("notes", []).append(
                f"{label} total capacity increase is {change_ratio * 100:.0f}%; "
                "verify this is within budget"
            )
