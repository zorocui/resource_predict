from __future__ import annotations

from typing import Any, Dict

import numpy as np


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


def build_k8s_pod_advice(
    metric_future_values: Dict[str, np.ndarray],
    *,
    resource: Dict[str, Any],
) -> Dict[str, Any]:
    spec = resource.get("spec", {})
    if not isinstance(spec, dict):
        spec = {}

    by_metric = {m: _stats(metric_future_values.get(m, np.array([]))) for m in ("cpu", "memory")}
    metric_actions: Dict[str, str] = {}
    metric_reasons: Dict[str, str] = {}
    blockers = []

    for metric in ("cpu", "memory"):
        st = by_metric[metric]
        label = "CPU" if metric == "cpu" else "内存"
        quality = _quality_level(resource, metric)
        has_base = _has_denominator(spec, metric)
        if quality == "poor":
            metric_actions[metric] = "insufficient_data"
            metric_reasons[metric] = f"{label} 数据质量较低，暂不建议调配"
            blockers.append(metric)
            continue
        if not has_base:
            metric_actions[metric] = "insufficient_data"
            metric_reasons[metric] = f"{label} 缺少 request/limit 基准，仅展示趋势"
            blockers.append(metric)
            continue
        if st["p95"] >= 0.8 or st["peak"] >= 0.9:
            metric_actions[metric] = "scale_out_candidate"
            metric_reasons[metric] = f"{label} 预测偏高(P95={st['p95'] * 100:.1f}%, 峰值={st['peak'] * 100:.1f}%)"
        elif st["avg"] < 0.2 and st["p95"] < 0.35:
            metric_actions[metric] = "scale_in_candidate"
            metric_reasons[metric] = f"{label} 预测偏低(均值={st['avg'] * 100:.1f}%, P95={st['p95'] * 100:.1f}%)"
        else:
            metric_actions[metric] = "hold"
            metric_reasons[metric] = f"{label} 负载在合理区间"

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
    confidence_score = max(0.0, min(100.0, confidence_score))
    confidence = "high" if confidence_score >= 72 else "medium" if confidence_score >= 45 else "low"

    reason = "；".join(metric_reasons[m] for m in ("cpu", "memory") if m in metric_reasons)
    return {
        "resource_type": "k8s_pod",
        "action": action,
        "reason": reason,
        "confidence": confidence,
        "confidence_score": round(confidence_score, 2),
        "metric_actions": metric_actions,
        "metric_reasons": metric_reasons,
        "stats": by_metric,
        "data_quality": resource.get("data_quality", {}),
        "target_k8s_policy": {},
        "analysis_only": True,
    }

