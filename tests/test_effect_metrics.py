"""Scaling outcomes use elapsed time and contemporaneous capacity, not row counts."""

import pytest

from resource_predict.services.scaling.effect_metrics import evaluate_series, summarize_events


HOUR = 3_600_000
POLICY = {"version": 1, "before_hours": 1, "after_hours": 1,
          "stabilization_minutes": 0, "min_coverage": 0.8, "max_gap_ms": HOUR}


def series(times, usages, capacities, container="", metric="cpu", basis="capacity"):
    return {"container": container, "metric": metric, "basis": basis,
            "unit": "cores" if metric == "cpu" else "GiB",
            "timestamps": [time * HOUR for time in times], "usage": usages, "capacity": capacities}


def evaluate(items, **kwargs):
    return evaluate_series(items, started_at_ms=HOUR, effective_at_ms=HOUR,
                           now_ms=kwargs.pop("now_ms", 2 * HOUR), policy=kwargs.pop("policy", POLICY), **kwargs)


def test_irregular_samples_are_time_weighted_with_observed_capacity():
    row = evaluate([series([0, .1, 1, 1.25, 2], [0, 1, 1, 2, 99], [2, 2, 1, 2, 99])])[0]
    assert row["status"] == "evaluated"
    assert row["before"]["mean_usage"] == pytest.approx(.9)
    assert row["before"]["utilization_pct"] == pytest.approx(45)
    assert row["after"]["mean_usage"] == pytest.approx(1.75)
    assert row["after"]["mean_capacity"] == pytest.approx(1.75)
    assert row["after"]["utilization_pct"] == pytest.approx(100)
    assert row["reclaimed_capacity"] == pytest.approx(.25)
    assert row["reclaimed_unit_hours"] == pytest.approx(.25)


def test_long_gap_skipped_entirely_and_no_tail_extrapolation():
    row = evaluate([series([0, .1, 1, 1.1, 1.2], [1] * 5, [2] * 5)],
                   policy={**POLICY, "max_gap_ms": .25 * HOUR})[0]
    assert row["status"] == "insufficient_data"
    assert row["before"]["coverage"] == pytest.approx(.1)
    assert row["after"]["coverage"] == pytest.approx(.2)
    assert row["after"]["utilization_pct"] == 50
    assert row["delta_pp"] is None


def test_clipping_does_not_turn_long_interval_into_valid_short_gap():
    row = evaluate([series([-10, .5, 1, 2], [99, 1, 1, 1], [100, 2, 1, 1])])[0]
    assert row["before"]["coverage"] == .5
    assert row["before"]["utilization_pct"] == 50
    assert row["status"] == "insufficient_data"


def test_stabilization_window_is_excluded_from_after_comparison():
    row = evaluate([series([0, 1, 2, 3], [1, 100, 1, 1], [2, 2, 1, 1])],
                   now_ms=3 * HOUR, policy={**POLICY, "stabilization_minutes": 60})[0]
    assert row["status"] == "evaluated"
    assert row["after"]["start_ms"] == 2 * HOUR
    assert row["after"]["end_ms"] == 3 * HOUR
    assert row["after"]["utilization_pct"] == 100
    assert row["after"]["overload_hours"] == 0


@pytest.mark.parametrize("usage,capacity", [(None, 2), (float("nan"), 2), (-1, 2),
                                           (1, 0), (1, -2), (1, float("inf"))])
def test_invalid_samples_are_missing_not_zero(usage, capacity):
    row = evaluate([series([0, 1, 2], [usage, usage, 1], [capacity, capacity, 2])])[0]
    assert row["status"] == "insufficient_data"
    assert row["before"]["coverage"] == 0
    assert row["before"]["utilization_pct"] is None
    assert row["before"]["p95_pct"] is None
    assert row["reclaimed_capacity"] is None


def test_zero_baseline_has_no_relative_change():
    row = evaluate([series([0, 1, 2], [0, 1, 1], [2, 1, 1])])[0]
    assert row["status"] == "evaluated"
    assert row["relative_change_pct"] is None
    assert row["delta_pp"] == 100
    assert row["capacity_reduction_pct"] == 50


def test_time_weighted_p95_and_overload_duration():
    row = evaluate([series([0, .01, 1, 2], [4, 1, 3, 0], [2, 2, 2, 2])])[0]
    assert row["before"]["p95_pct"] == 50
    assert row["before"]["overload_hours"] == pytest.approx(.01)
    assert row["after"]["p95_pct"] == 150
    assert row["after"]["overload_hours"] == 1


def test_complete_container_aggregate_uses_sum_of_contemporaneous_values():
    rows = evaluate([series([0, 1, 2], [1, 1, 1], [2, 1, 1], "app", basis="request"),
                     series([0, 1, 2], [2, 2, 2], [8, 4, 4], "worker", basis="request")])
    assert len(rows) == 3
    aggregate = next(row for row in rows if row["container"] == "")
    assert aggregate["before"]["utilization_pct"] == 30
    assert aggregate["after"]["utilization_pct"] == 60
    assert aggregate["reclaimed_capacity"] == 5


def test_expected_missing_container_never_creates_partial_workload_success():
    rows = evaluate([series([0, 1, 2], [1, 1, 1], [2, 1, 1], "app", basis="request")],
                    policy={**POLICY, "expected_containers": ["app", "sidecar"]})
    aggregate, individual = rows
    assert individual["status"] == "evaluated"
    assert aggregate["status"] == "insufficient_data"
    assert aggregate["before"]["mean_capacity"] is None


def test_missing_timestamp_does_not_bridge_to_create_false_full_coverage():
    rows = evaluate([series([0, .5, 1, 1.5, 2], [1] * 5, [2] * 5, "app", basis="limit"),
                     series([0, 1, 2], [1] * 3, [2] * 3, "sidecar", basis="limit")])
    aggregate = next(row for row in rows if row["container"] == "")
    assert aggregate["before"]["coverage"] == .5
    assert aggregate["after"]["coverage"] == .5
    assert aggregate["status"] == "insufficient_data"


def test_no_certification_before_window_end_or_after_interruption():
    samples = [series([0, 1, 2], [1, 1, 1], [2, 1, 1])]
    assert evaluate(samples, now_ms=int(1.9 * HOUR))[0]["status"] == "insufficient_data"
    interrupted = evaluate(samples, interrupted_at_ms=int(1.5 * HOUR))[0]
    assert interrupted["status"] == "interrupted"
    assert interrupted["delta_pp"] is None
    assert evaluate(samples, interrupted_at_ms=2 * HOUR)[0]["status"] == "evaluated"


def event(identifier, row, **kwargs):
    return {"task_id": identifier, "resource_id": "same-resource", "resource_type": "vm",
            "action": "shrink", "status": "evaluated", "metrics": [row], **kwargs}


def test_summary_fixed_pre_weights_keep_negative_results_and_event_counts():
    first = evaluate([series([0, 1, 2], [1, 1, 1], [2, 1, 1])])[0]
    second = evaluate([series([0, 1, 2], [4, 4, 4], [8, 16, 16])])[0]
    summary = summarize_events([event("one", first), event("two", second)])
    row = summary["metrics"][0]
    assert summary["event_count"] == 2
    assert summary["resource_count"] == 1
    assert row["event_count"] == 2
    assert row["resource_count"] == 1
    assert row["before_pct"] == 50
    assert row["after_pct"] == 40  # (100 * 2 + 25 * 8) / 10; AFTER weights are wrong.
    assert row["delta_pp"] == -10
    assert row["weighted_relative_change_pct"] == -20
    assert row["mean_relative_change_pct"] == 25
    assert row["reclaimed_capacity"] == -7
    assert row["reclaimed_unit_hours"] == -7


def test_summary_zero_baseline_excluded_from_relative_count_only():
    row = evaluate([series([0, 1, 2], [0, 1, 1], [2, 1, 1])])[0]
    summary = summarize_events([event("one", row)])["metrics"][0]
    assert summary["relative_count"] == 0
    assert summary["mean_relative_change_pct"] is None
    assert summary["weighted_relative_change_pct"] is None
    assert summary["event_count"] == 1
    assert summary["reclaimed_capacity"] == 1


def test_summary_isolates_units_basis_actions_and_filters_ineligible_rows():
    cpu = evaluate([series([0, 1, 2], [1, 1, 1], [2, 1, 1])])[0]
    memory = evaluate([series([0, 1, 2], [1, 1, 1], [2, 1, 1], metric="memory")])[0]
    events = [event("cpu", cpu), event("memory", memory), event("expand", cpu, action="expand"),
              event("failed", cpu, status="failed"), event("container", {**cpu, "container": "app"}),
              event("pending", {**cpu, "status": "insufficient_data"})]
    summary = summarize_events(events)
    assert len(summary["metrics"]) == 3
    assert summary["event_count"] == 6
    assert summary["status_counts"]["failed"] == 1
    assert all(row["event_count"] == 1 for row in summary["metrics"])
    assert summarize_events([]) == {"event_count": 0, "resource_count": 0, "status_counts": {}, "metrics": []}
