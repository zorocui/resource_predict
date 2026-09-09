"""Observed scaling outcomes from raw, contemporaneous usage/capacity samples.

Intervals are left-continuous, never extrapolated or filled across long gaps.
Capacity reclamation is signed (expansion is negative), not a causal saving.
"""

from collections import Counter, defaultdict
from math import isfinite


HOUR_MS = 3_600_000


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _valid(usage, capacity):
    return _number(usage) and usage >= 0 and _number(capacity) and capacity > 0


def _points(series):
    """Deduplicate timestamps without deleting invalid samples or hiding gaps."""
    return dict((timestamp, (usage, capacity)) for timestamp, usage, capacity in zip(
        series.get("timestamps", []), series.get("usage", []), series.get("capacity", [])
    ) if _number(timestamp))


def _window(points, start, end, max_gap, observed_until):
    valid_ms = 0
    usage_time = capacity_time = overload_ms = 0.0
    percentiles = []
    ordered = sorted(points.items())
    for (timestamp, (usage, capacity)), (next_timestamp, _) in zip(ordered, ordered[1:]):
        # A future right-hand sample is not evidence available at evaluation time.
        if next_timestamp > observed_until or next_timestamp - timestamp > max_gap:
            continue
        duration = min(next_timestamp, end) - max(timestamp, start)
        if duration <= 0 or not _valid(usage, capacity):
            continue
        utilization = usage / capacity * 100
        valid_ms += duration
        usage_time += usage * duration
        capacity_time += capacity * duration
        overload_ms += duration if utilization > 100 else 0
        percentiles.append((utilization, duration))
    p95 = None
    cumulative = 0
    for utilization, duration in sorted(percentiles):
        cumulative += duration
        if cumulative >= valid_ms * 0.95:
            p95 = utilization
            break
    return {
        "start_ms": start, "end_ms": end,
        "valid_hours": valid_ms / HOUR_MS,
        "coverage": valid_ms / (end - start) if end > start else 0.0,
        "mean_usage": usage_time / valid_ms if valid_ms else None,
        "mean_capacity": capacity_time / valid_ms if valid_ms else None,
        "utilization_pct": usage_time / capacity_time * 100 if capacity_time else None,
        "p95_pct": p95,
        "overload_hours": overload_ms / HOUR_MS if valid_ms else None,
    }


def evaluate_series(series: list[dict], *, started_at_ms: int, effective_at_ms: int,
                    policy: dict, now_ms: int, interrupted_at_ms: int | None = None) -> list[dict]:
    """Evaluate each container and the complete Workload cohort independently.

    ``expected_containers`` freezes the cohort from the execution's before spec.
    Aggregate samples require exact timestamp matches for every expected name;
    missing containers/values produce missing data, never smaller denominators.
    This function does not infer or certify attainment of a declared target.
    """
    before_start = started_at_ms - policy.get("before_hours", 24) * HOUR_MS
    after_start = effective_at_ms + policy.get("stabilization_minutes", 60) * 60_000
    after_end = after_start + policy.get("after_hours", 24) * HOUR_MS
    interrupted = interrupted_at_ms is not None and interrupted_at_ms < after_end
    observed_until = min(now_ms, interrupted_at_ms) if interrupted else now_ms
    max_gap = policy.get("max_gap_ms", 900_000)
    coverage = policy.get("min_coverage", 0.8)
    grouped = defaultdict(dict)
    for item in series:
        key = (item.get("container", ""), item["metric"], item["basis"], item["unit"])
        grouped[key].update(_points(item))

    cohorts = defaultdict(dict)
    for (container, metric, basis, unit), points in grouped.items():
        if container:
            cohorts[(metric, basis, unit)][container] = points
    for (metric, basis, unit), containers in cohorts.items():
        expected = set(policy.get("expected_containers") or containers)
        timestamps = set().union(*(points.keys() for points in containers.values()))
        aggregate = {}
        for timestamp in timestamps:
            values = [containers.get(name, {}).get(timestamp, (None, None)) for name in expected]
            aggregate[timestamp] = (
                (sum(value[0] for value in values), sum(value[1] for value in values))
                if values and all(_valid(*value) for value in values) else (None, None)
            )
        grouped[("", metric, basis, unit)] = aggregate

    results = []
    for (container, metric, basis, unit), points in sorted(grouped.items()):
        before = _window(points, before_start, started_at_ms, max_gap, now_ms)
        after = _window(points, after_start, after_end, max_gap, observed_until)
        status = "insufficient_data"
        if interrupted:
            status = "interrupted"
        elif now_ms >= after_end and before["coverage"] >= coverage and after["coverage"] >= coverage:
            if before["mean_capacity"] is not None and after["mean_capacity"] is not None:
                status = "evaluated"
        result = {
            "container": container, "metric": metric, "basis": basis, "unit": unit,
            "status": status, "before": before, "after": after,
            "delta_pp": None, "relative_change_pct": None, "reclaimed_capacity": None,
            "capacity_reduction_pct": None, "reclaimed_unit_hours": None,
        }
        if status == "evaluated":
            delta = after["utilization_pct"] - before["utilization_pct"]
            reclaimed = before["mean_capacity"] - after["mean_capacity"]
            result.update(
                delta_pp=delta,
                relative_change_pct=delta / before["utilization_pct"] * 100 if before["utilization_pct"] else None,
                reclaimed_capacity=reclaimed,
                capacity_reduction_pct=reclaimed / before["mean_capacity"] * 100,
                reclaimed_unit_hours=reclaimed * after["valid_hours"],
            )
        results.append(result)
    return results


def summarize_events(events: list[dict]) -> dict:
    """Summarize eligible aggregate observations using fixed PRE capacity weights.

    Counts are events, with separate distinct resource counts. CPU/memory,
    request/limit/capacity, resource type and action are never blended together.
    Negative outcomes remain in totals; zero-baseline relative change is absent.
    """
    groups = defaultdict(list)
    for event in events:
        if event.get("status") != "evaluated":
            continue
        for row in event.get("metrics", []):
            if row.get("container", "") or row.get("status") != "evaluated":
                continue
            weight = row["before"].get("mean_capacity")
            if not _number(weight) or weight <= 0:
                continue
            if not all(_number(row[window].get("utilization_pct")) for window in ("before", "after")):
                continue
            key = (event.get("resource_type", ""), event.get("action", ""), row["metric"], row["basis"])
            groups[key].append((event, row))
    metrics = []
    for (resource_type, action, metric, basis), rows in sorted(groups.items()):
        weight = sum(row["before"]["mean_capacity"] for _, row in rows)
        before = sum(row["before"]["utilization_pct"] * row["before"]["mean_capacity"] for _, row in rows) / weight
        after = sum(row["after"]["utilization_pct"] * row["before"]["mean_capacity"] for _, row in rows) / weight
        relatives = [row["relative_change_pct"] for _, row in rows if _number(row.get("relative_change_pct"))]
        reclaimed = [row["reclaimed_capacity"] for _, row in rows if _number(row.get("reclaimed_capacity"))]
        unit_hours = [row["reclaimed_unit_hours"] for _, row in rows if _number(row.get("reclaimed_unit_hours"))]
        metrics.append({
            "resource_type": resource_type, "action": action, "metric": metric, "basis": basis,
            "unit": rows[0][1]["unit"], "event_count": len(rows),
            "resource_count": len({event.get("resource_id") for event, _ in rows}),
            "relative_count": len(relatives), "before_pct": before, "after_pct": after,
            "delta_pp": after - before,
            "mean_relative_change_pct": sum(relatives) / len(relatives) if relatives else None,
            "weighted_relative_change_pct": (after - before) / before * 100 if before else None,
            "reclaimed_capacity": sum(reclaimed) if reclaimed else None,
            "reclaimed_unit_hours": sum(unit_hours) if unit_hours else None,
        })
    return {
        "event_count": len(events),
        "resource_count": len({(event.get("resource_type"), event.get("resource_id")) for event in events}),
        "status_counts": dict(Counter(event.get("status", "unknown") for event in events)),
        "metrics": metrics,
    }
