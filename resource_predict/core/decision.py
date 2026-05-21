from __future__ import annotations

import math
from typing import Dict, List

import numpy as np

from resource_predict.settings import settings


def _normalize_vm_spec(vm_spec: Dict[str, object]) -> Dict[str, int]:
    def _as_int(v: object) -> int:
        try:
            return int(float(v))
        except Exception:
            return 0

    return {
        "cpu_cores": _as_int((vm_spec or {}).get("cpu_cores")),
        "memory_gb": _as_int((vm_spec or {}).get("memory_gb")),
        "disk_gb": _as_int((vm_spec or {}).get("disk_gb")),
    }


_METRIC_TO_DIM = {
    "cpu": "cpu_cores",
    "memory": "memory_gb",
    "disk": "disk_gb",
}


def _metric_is_hot(stats: Dict[str, float]) -> bool:
    """与 build_scaling_advice 中 out_reasons 同口径，判断该维度是否真正高负载。"""
    cfg = settings.decision
    p95 = float(stats.get("p95", 0.0))
    peak = float(stats.get("peak", 0.0))
    gap = float(stats.get("gap", 0.0))
    slope = float(stats.get("slope", 0.0))
    delta = float(stats.get("window_mean_delta", 0.0))
    if p95 >= float(cfg.scale_out_threshold):
        return True
    if peak >= float(cfg.peak_guard_threshold) and gap >= float(cfg.peak_valley_gap_threshold):
        return True
    if slope >= float(cfg.uptrend_slope_threshold) and delta >= float(cfg.window_mean_delta_threshold):
        return True
    return False


def _metric_is_cold(stats: Dict[str, float]) -> bool:
    """与 build_scaling_advice 中 in_reasons 同口径，判断该维度是否真正低负载。"""
    cfg = settings.decision
    avg = float(stats.get("avg", 0.0))
    p95 = float(stats.get("p95", 0.0))
    return avg < float(cfg.scale_in_threshold) and p95 < float(cfg.scale_in_p95_guard)


def _finalize_target_vm_spec_even(action: str, target: Dict[str, int], disk_min_gb: int = 50) -> Dict[str, int]:
    """扩容/缩容建议中的规格对齐为偶数（奇数则 +1），贴近常见可购规格。
    
    硬盘缩容时最小规格为 disk_min_gb（默认50GB）。
    """
    cfg = settings.decision
    if not bool(cfg.snap_target_cpu_cores_to_even):
        return target
    if action not in ("scale_out", "scale_in"):
        return target
    
    result = {}
    for dim in ("cpu_cores", "memory_gb", "disk_gb"):
        val = int(target.get(dim, 0))
        if val <= 0:
            result[dim] = val
            continue
        
        # 对齐为偶数：奇数则 +1
        if val % 2 == 1:
            val = val + 1
        
        # 硬盘缩容时应用最小规格限制
        if dim == "disk_gb" and action == "scale_in":
            val = max(val, disk_min_gb)
        
        result[dim] = val
    
    return result


def _pick_target_vm_spec_by_metric(
    current_vm_spec: Dict[str, object],
    by_metric: Dict[str, Dict[str, float]],
    metric_actions: Dict[str, str],
) -> Dict[str, int]:
    """
    基于「每个指标的动作」+ 当前规格 + 预测统计，计算建议目标规格（直接给出整数核数/GB，不映射固定机型表）。

    规则摘要（按维度分别决策）：
    - metric_actions[metric] == scale_out：仅该维度放大；若 max(P95,峰值) 超过 100%（>1），
      按「当前容量 × 负载 / 目标利用率」推算并与分档系数取较大者；否则仅用分档系数；
    - metric_actions[metric] == scale_in：仅该维度按 0.7 安全余量缩容；
    - metric_actions[metric] == hold：该维度保持当前规格。
    - 最终建议核数默认可对齐为偶数（见 snap_target_cpu_cores_to_even）。
    """
    cur = _normalize_vm_spec(current_vm_spec)
    if not all(cur[k] > 0 for k in ("cpu_cores", "memory_gb", "disk_gb")):
        return {}

    target: Dict[str, int] = {dim: int(cur[dim]) for dim in cur}

    cfg = settings.decision
    tgt_util = max(float(cfg.scale_out_target_utilization), 0.05)
    load_threshold = float(cfg.scale_out_capacity_load_threshold)

    def _out_factor(p95: float) -> float:
        if p95 >= 0.95:
            return 2.0
        if p95 >= 0.9:
            return 1.7
        if p95 >= 0.85:
            return 1.5
        return 1.25

    for metric, dim in _METRIC_TO_DIM.items():
        stats = by_metric.get(metric) or {}
        metric_action = str(metric_actions.get(metric, "hold"))
        if metric_action == "scale_out":
            p95 = float(stats.get("p95", 0.0))
            peak = float(stats.get("peak", 0.0))
            # 监控口径下使用率可超过 100%（如多核累加、超卖等），此时按等效负载线性推算容量，
            # 避免仅按分档系数（最大 2x）扩容后仍高于 100%。
            load = max(p95, peak)
            bucket_v = int(math.ceil(cur[dim] * _out_factor(p95)))
            if load > load_threshold:
                proportional_v = int(math.ceil(cur[dim] * load / tgt_util))
                new_v = max(proportional_v, bucket_v, cur[dim])
            else:
                new_v = max(bucket_v, cur[dim])
            target[dim] = new_v
            continue
        if metric_action == "scale_in":
            p95 = float(stats.get("p95", 0.0))
            # 缩容后希望 P95 大致仍落在 0.7（70%）以内，避免缩过头。
            min_need = int(math.ceil(cur[dim] * max(p95, 0.01) / 0.7))
            # 加入单步保护：当前容量 × (1 - max_reduction_ratio)，防止过激单步缩容
            max_reduction = float(cfg.scale_in_max_reduction_ratio)
            reduction_floor = max(1, int(cur[dim] * (1.0 - max_reduction)))
            target[dim] = max(reduction_floor, min(min_need, cur[dim]))

    has_out = any(v == "scale_out" for v in metric_actions.values())
    has_in = any(v == "scale_in" for v in metric_actions.values())
    if has_out:
        overall_action = "scale_out"
    elif has_in:
        overall_action = "scale_in"
    else:
        overall_action = "hold"
    return _finalize_target_vm_spec_even(overall_action, target)


def _reconcile_noop_metric_actions(
    *,
    current_vm_spec: Dict[str, object],
    target_vm_spec: Dict[str, int],
    metric_actions: Dict[str, str],
    metric_reasons: Dict[str, str],
) -> None:
    cur = _normalize_vm_spec(current_vm_spec)
    target = _normalize_vm_spec(target_vm_spec)
    if not all(cur[k] > 0 for k in ("cpu_cores", "memory_gb", "disk_gb")):
        return
    if not all(target[k] > 0 for k in ("cpu_cores", "memory_gb", "disk_gb")):
        return

    metric_label = {"cpu": "CPU", "memory": "内存", "disk": "硬盘"}
    for metric, dim in _METRIC_TO_DIM.items():
        if metric_actions.get(metric) not in {"scale_out", "scale_in"}:
            continue
        if int(target.get(dim, 0)) == int(cur.get(dim, 0)):
            metric_actions[metric] = "hold"
            metric_reasons[metric] = f"{metric_label[metric]}目标规格与当前规格一致，建议保持"


def _summarize_metric_actions(metric_actions: Dict[str, str]) -> Dict[str, object]:
    out_metrics = [m for m in ("cpu", "memory", "disk") if metric_actions[m] == "scale_out"]
    in_metrics = [m for m in ("cpu", "memory", "disk") if metric_actions[m] == "scale_in"]
    has_mixed = bool(out_metrics) and bool(in_metrics)
    action = "hold"
    if out_metrics:
        action = "scale_out"
    elif in_metrics:
        action = "scale_in"
    if action == "scale_out":
        suggested_delta = 1
    elif action == "scale_in" and not out_metrics:
        suggested_delta = -1
    else:
        suggested_delta = 0
    return {
        "out_metrics": out_metrics,
        "in_metrics": in_metrics,
        "has_mixed": has_mixed,
        "action": action,
        "suggested_delta": suggested_delta,
    }


def _max_consecutive(values: np.ndarray, predicate) -> int:
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


def _metric_stats(values: np.ndarray) -> Dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return {
            "avg": 0.0,
            "p95": 0.0,
            "peak": 0.0,
            "valley": 0.0,
            "gap": 0.0,
            "std": 0.0,
        }
    peak = float(np.max(arr))
    valley = float(np.min(arr))
    return {
        "avg": float(np.mean(arr)),
        "p95": float(np.percentile(arr, 95)),
        "peak": peak,
        "valley": valley,
        "gap": peak - valley,
        "std": float(np.std(arr)),
    }


def _trend_features(values: np.ndarray, window: int) -> Dict[str, float]:
    arr = np.asarray(values, dtype=float)
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


def _bounded_above(value: float, threshold: float, *, soft_cap: float = 1.0) -> float:
    """0..1+ score for exceeding a threshold, with gentle differentiation above 100%."""
    threshold = max(float(threshold), 0.001)
    value = float(value)
    if value <= threshold:
        return 0.0
    if value <= soft_cap:
        return min(1.0, (value - threshold) / max(soft_cap - threshold, 0.001))
    return 1.0 + min(1.0, math.log1p(value - soft_cap))


def _bounded_below(value: float, threshold: float) -> float:
    """0..1 score for being safely below a threshold."""
    threshold = max(float(threshold), 0.001)
    return max(0.0, min(1.0, (threshold - float(value)) / threshold))


def _trend_support_score(st: Dict[str, float], *, direction: str) -> float:
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


def _metric_confidence_score(metric_action: str, st: Dict[str, float]) -> float:
    """Return a 0..100 reliability score for one metric's scaling signal."""
    cfg = settings.decision
    avg = float(st.get("avg", 0.0))
    p95 = float(st.get("p95", 0.0))
    peak = float(st.get("peak", 0.0))
    gap = float(st.get("gap", 0.0))
    high_ratio = float(st.get("high_ratio", 0.0))
    low_ratio = float(st.get("low_ratio", 0.0))

    if metric_action == "scale_out":
        p95_strength = _bounded_above(p95, float(cfg.scale_out_threshold))
        peak_strength = _bounded_above(peak, float(cfg.peak_guard_threshold))
        avg_strength = _bounded_above(avg, float(cfg.scale_out_threshold))
        persistence = max(high_ratio, min(1.0, p95_strength))
        trend = _trend_support_score(st, direction="up")
        spike_penalty = 0.0
        if peak_strength > 0.0 and p95_strength < 0.15:
            spike_penalty = min(18.0, 18.0 * min(1.0, gap / max(float(cfg.peak_valley_gap_threshold), 0.001)))
        score = (
            42.0 * min(1.0, p95_strength)
            + 20.0 * min(1.0, peak_strength)
            + 14.0 * min(1.0, avg_strength)
            + 16.0 * persistence
            + 8.0 * trend
            + 8.0 * max(0.0, p95_strength - 1.0)
            - spike_penalty
        )
        return max(0.0, min(100.0, score))

    if metric_action == "scale_in":
        avg_headroom = _bounded_below(avg, float(cfg.scale_in_threshold))
        p95_headroom = _bounded_below(p95, float(cfg.scale_in_p95_guard))
        trend = _trend_support_score(st, direction="down")
        stability = 1.0 - min(1.0, gap / max(float(cfg.scale_in_p95_guard), 0.001))
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


def _overall_confidence(
    metric_actions: Dict[str, str],
    by_metric: Dict[str, Dict[str, float]],
    *,
    has_mixed_signals: bool,
) -> Dict[str, object]:
    scores = [
        _metric_confidence_score(metric_actions[m], by_metric[m])
        for m in ("cpu", "memory", "disk")
        if metric_actions.get(m) in {"scale_out", "scale_in"}
    ]
    if not scores:
        return {"label": "medium", "score": 50.0, "metric_scores": {}}

    primary = max(scores)
    avg = float(np.mean(scores))
    score = 0.65 * primary + 0.35 * avg
    if len(scores) >= 2:
        score += 4.0
    if has_mixed_signals:
        score -= 8.0
    score = max(0.0, min(100.0, score))

    if score >= 72.0:
        label = "high"
    elif score >= 45.0:
        label = "medium"
    else:
        label = "low"
    metric_scores = {
        m: round(_metric_confidence_score(metric_actions[m], by_metric[m]), 2)
        for m in ("cpu", "memory", "disk")
        if metric_actions.get(m) in {"scale_out", "scale_in"}
    }
    return {"label": label, "score": round(score, 2), "metric_scores": metric_scores}


def build_scaling_advice(
    metric_future_values: Dict[str, np.ndarray],
    current_vm_spec: Dict[str, object] | None = None,
) -> Dict[str, object]:
    """
    根据未来窗口负载预测生成扩缩容建议。

    返回结构：
    - action: scale_out / scale_in / hold（总体动作，便于列表筛选）
    - reason: 人类可读的核心判定依据（包含分指标动作）
    - confidence: high / medium / low
    - suggested_delta: +1 / -1 / 0（动作方向）
    - metric_actions: 各指标动作（cpu/memory/disk -> scale_out/scale_in/hold）
    - metric_reasons: 各指标判定依据（便于前端按维度展示）
    - target_vm_spec: 目标规格（cpu_cores/memory_gb/disk_gb）
    - stats: 每个指标的统计与趋势特征（avg/p95/peak/gap/slope/...）
    """
    cfg = settings.decision
    by_metric: Dict[str, Dict[str, float]] = {}
    out_reasons: List[str] = []
    in_reasons: List[str] = []
    metric_actions: Dict[str, str] = {}
    metric_reasons: Dict[str, str] = {}
    metric_label = {"cpu": "CPU", "memory": "内存", "disk": "硬盘"}

    for metric in ("cpu", "memory", "disk"):
        vals = np.asarray(metric_future_values.get(metric, np.array([])), dtype=float)
        st = _metric_stats(vals)
        tr = _trend_features(vals, cfg.trend_window_points)
        st.update(tr)
        high_streak = _max_consecutive(vals, lambda x: x >= cfg.consecutive_high_threshold)
        low_streak = _max_consecutive(vals, lambda x: x <= cfg.consecutive_low_threshold)
        if vals.size > 0:
            st["high_ratio"] = float(np.mean(vals >= cfg.consecutive_high_threshold))
            st["low_ratio"] = float(np.mean(vals <= cfg.consecutive_low_threshold))
        else:
            st["high_ratio"] = 0.0
            st["low_ratio"] = 0.0
        st["high_streak"] = float(high_streak)
        st["low_streak"] = float(low_streak)
        by_metric[metric] = st

        if st["p95"] >= cfg.scale_out_threshold:
            out_reasons.append(f"{metric}:P95={st['p95'] * 100:.1f}%")
        if high_streak >= cfg.consecutive_points:
            out_reasons.append(f"{metric}:连续高负载{high_streak}点")
        if st["peak"] >= cfg.peak_guard_threshold and st["gap"] >= cfg.peak_valley_gap_threshold:
            out_reasons.append(
                f"{metric}:峰值{st['peak'] * 100:.1f}%且峰谷差{st['gap'] * 100:.1f}%"
            )
        if st["slope"] >= cfg.uptrend_slope_threshold and st["window_mean_delta"] >= cfg.window_mean_delta_threshold:
            out_reasons.append(
                f"{metric}:上升趋势明显(slope={st['slope']:.4f},Δmean={st['window_mean_delta'] * 100:.1f})"
            )

        if st["avg"] < cfg.scale_in_threshold and st["p95"] < cfg.scale_in_p95_guard:
            in_reasons.append(f"{metric}:均值{st['avg'] * 100:.1f}%且P95={st['p95'] * 100:.1f}%")
        if low_streak >= cfg.consecutive_points:
            in_reasons.append(f"{metric}:连续低负载{low_streak}点")
        if st["slope"] <= cfg.downtrend_slope_threshold and st["window_mean_delta"] <= -cfg.window_mean_delta_threshold:
            in_reasons.append(
                f"{metric}:下降趋势明显(slope={st['slope']:.4f},Δmean={st['window_mean_delta'] * 100:.1f})"
            )

    for metric in ("cpu", "memory", "disk"):
        st = by_metric[metric]
        is_hot = _metric_is_hot(st)
        is_cold = _metric_is_cold(st)
        has_strong_uptrend = st["slope"] >= cfg.uptrend_slope_threshold and st["window_mean_delta"] >= cfg.window_mean_delta_threshold
        label = metric_label[metric]

        if is_hot:
            metric_actions[metric] = "scale_out"
            metric_reasons[metric] = f"{label}预测偏高(P95={st['p95'] * 100:.1f}%,峰值={st['peak'] * 100:.1f}%)，建议扩容"
        elif is_cold:
            if has_strong_uptrend:
                metric_actions[metric] = "hold"
                metric_reasons[metric] = (
                    f"{label}当前偏低(均值{st['avg'] * 100:.1f}%,P95={st['p95'] * 100:.1f}%)，"
                    "但趋势回升，暂缓缩容"
                )
            else:
                metric_actions[metric] = "scale_in"
                metric_reasons[metric] = f"{label}预测偏低(均值{st['avg'] * 100:.1f}%,P95={st['p95'] * 100:.1f}%)，建议缩容"
        else:
            metric_actions[metric] = "hold"
            metric_reasons[metric] = f"{label}负载在合理区间，建议保持"

    target_vm_spec = _pick_target_vm_spec_by_metric(current_vm_spec or {}, by_metric, metric_actions)
    _reconcile_noop_metric_actions(
        current_vm_spec=current_vm_spec or {},
        target_vm_spec=target_vm_spec,
        metric_actions=metric_actions,
        metric_reasons=metric_reasons,
    )
    target_vm_spec = _pick_target_vm_spec_by_metric(current_vm_spec or {}, by_metric, metric_actions)
    _reconcile_noop_metric_actions(
        current_vm_spec=current_vm_spec or {},
        target_vm_spec=target_vm_spec,
        metric_actions=metric_actions,
        metric_reasons=metric_reasons,
    )
    action_info = _summarize_metric_actions(metric_actions)
    action = str(action_info["action"])
    has_mixed = bool(action_info["has_mixed"])
    suggested_delta = int(action_info["suggested_delta"])
    action_phrase = {"scale_out": "扩容", "scale_in": "缩容", "hold": "保持"}
    reason_parts = [
        f"{metric_label[m]}{action_phrase[metric_actions[m]]}" for m in ("cpu", "memory", "disk")
    ]
    reason = "；".join(reason_parts)

    confidence_info = _overall_confidence(
        metric_actions,
        by_metric,
        has_mixed_signals=has_mixed,
    )
    confidence = str(confidence_info["label"])

    return {
        "action": action,
        "reason": reason,
        "confidence": confidence,
        "confidence_score": confidence_info["score"],
        "confidence_metric_scores": confidence_info["metric_scores"],
        "suggested_delta": suggested_delta,
        "metric_actions": metric_actions,
        "metric_reasons": metric_reasons,
        "target_vm_spec": target_vm_spec,
        "stats": by_metric,
        "has_mixed_signals": has_mixed,
    }
