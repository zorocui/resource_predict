from __future__ import annotations

import math
from typing import Any, Dict, List


def compute_urgency_score(item: Dict[str, Any], cfg: Any) -> float:
    """Compute list sorting urgency from scaling advice and target spec changes."""
    advice = item.get("scaling_advice", {}) if isinstance(item, dict) else {}
    if not isinstance(advice, dict):
        return 0.0
    action = str(advice.get("action", "hold")).lower()
    confidence = str(advice.get("confidence", "medium")).lower()
    if action == "hold":
        return 0.0
    if action == "insufficient_data":
        return 1.0
    stats = advice.get("stats", {})
    if not isinstance(stats, dict):
        stats = {}

    metric_actions = advice.get("metric_actions", {})
    if not isinstance(metric_actions, dict):
        metric_actions = {}

    def _num(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, v))

    def _above(value: float, threshold: float, *, soft_cap: float = 1.0) -> float:
        threshold = max(float(threshold), 0.001)
        if value <= threshold:
            return 0.0
        if value <= soft_cap:
            return _clamp((value - threshold) / max(soft_cap - threshold, 0.001))
        return 1.0 + min(2.0, math.log1p(value - soft_cap))

    def _below(value: float, threshold: float) -> float:
        threshold = max(float(threshold), 0.001)
        return _clamp((threshold - value) / threshold)

    def _target_change_score() -> float:
        spec = item.get("spec", {})
        target = advice.get("target_spec", {})
        if not isinstance(spec, dict) or not isinstance(target, dict):
            return 0.0
        ratios = []
        for dim in ("cpu_cores", "memory_gb", "disk_gb"):
            cur = _num(spec.get(dim), 0.0)
            nxt = _num(target.get(dim), 0.0)
            if cur <= 0 or nxt <= 0:
                continue
            if action == "scale_out":
                ratios.append(max(nxt / cur - 1.0, 0.0))
            elif action == "scale_in":
                ratios.append(max(1.0 - nxt / cur, 0.0))
        return min(18.0, 18.0 * max(ratios, default=0.0))

    confidence_bonus = {
        "high": 6.0,
        "medium": 3.0,
        "low": 1.0,
    }.get(confidence, 2.0)

    metric_scores: List[float] = []
    for metric in ("cpu", "memory", "disk"):
        st = stats.get(metric, {})
        if not isinstance(st, dict):
            continue
        metric_action = str(metric_actions.get(metric, action)).lower()
        avg = _num(st.get("avg"))
        p95 = _num(st.get("p95"))
        peak = _num(st.get("peak"))
        gap = _num(st.get("gap"))
        slope = _num(st.get("slope"))
        delta = _num(st.get("window_mean_delta"))

        if metric_action in {"scale_out", "scale_out_candidate"}:
            trend_pressure = 0.0
            if slope > 0:
                trend_pressure += min(1.0, slope / max(float(cfg.uptrend_slope_threshold), 0.0001))
            if delta > 0:
                trend_pressure += min(1.0, delta / max(float(cfg.window_mean_delta_threshold), 0.0001))
            metric_scores.append(
                32.0 * _above(p95, float(cfg.scale_out_threshold))
                + 22.0 * _above(peak, float(cfg.peak_guard_threshold))
                + 12.0 * _above(avg, float(cfg.scale_out_threshold))
                + 6.0 * trend_pressure
                + 4.0 * min(1.0, gap / max(float(cfg.peak_valley_gap_threshold), 0.0001))
            )
        elif metric_action in {"scale_in", "scale_in_candidate"}:
            trend_pressure = 0.0
            if slope < 0:
                trend_pressure += min(1.0, abs(slope) / max(abs(float(cfg.downtrend_slope_threshold)), 0.0001))
            if delta < 0:
                trend_pressure += min(1.0, abs(delta) / max(float(cfg.window_mean_delta_threshold), 0.0001))
            metric_scores.append(
                20.0 * _below(avg, float(cfg.scale_in_threshold))
                + 16.0 * _below(p95, float(cfg.scale_in_p95_guard))
                + 5.0 * trend_pressure
                + 4.0 * (1.0 - min(1.0, gap / 0.5))
            )

    if not metric_scores:
        return confidence_bonus

    return round(
        (35.0 if action in {"scale_out", "scale_out_candidate"} else 18.0)
        + confidence_bonus
        + max(metric_scores)
        + 0.25 * sum(sorted(metric_scores, reverse=True)[1:])
        + 4.0 * max(0, len(metric_scores) - 1)
        + (4.0 if bool(advice.get("has_mixed_signals")) else 0.0)
        + _target_change_score(),
        3,
    )

