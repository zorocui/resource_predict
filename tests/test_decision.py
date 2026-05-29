from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np

from resource_predict.core.decision import (
    _bounded_above,
    _bounded_below,
    _finalize_target_spec_even,
    _max_consecutive,
    _metric_confidence_score,
    _metric_is_cold,
    _metric_is_hot,
    _normalize_spec,
    _policy_thresholds,
    _summarize_metric_actions,
    _trend_features,
    build_scaling_advice,
)
from resource_predict.settings import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future(metric_values):
    return {
        "cpu": np.asarray(metric_values.get("cpu", [0.5] * 24)),
        "memory": np.asarray(metric_values.get("memory", [0.5] * 24)),
        "disk": np.asarray(metric_values.get("disk", [0.3] * 24)),
    }


VM_SPEC = {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDecision(unittest.TestCase):
    """Comprehensive unit tests for resource_predict.core.decision."""

    # -- _normalize_spec -----------------------------------------------------

    def test_normalize_spec_valid(self):
        result = _normalize_spec({"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100})
        self.assertEqual(result, {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100})

    def test_normalize_spec_missing_keys(self):
        result = _normalize_spec({"cpu_cores": 2})
        self.assertEqual(result["cpu_cores"], 2)
        self.assertEqual(result["memory_gb"], 0)
        self.assertEqual(result["disk_gb"], 0)

    def test_normalize_spec_none_input(self):
        result = _normalize_spec(None)
        self.assertEqual(result, {"cpu_cores": 0, "memory_gb": 0, "disk_gb": 0})

    def test_normalize_spec_invalid_values(self):
        result = _normalize_spec({"cpu_cores": "abc", "memory_gb": None, "disk_gb": []})
        self.assertEqual(result, {"cpu_cores": 0, "memory_gb": 0, "disk_gb": 0})

    def test_normalize_spec_string_and_float(self):
        result = _normalize_spec({"cpu_cores": "4.7", "memory_gb": 8.9, "disk_gb": "100"})
        self.assertEqual(result["cpu_cores"], 4)
        self.assertEqual(result["memory_gb"], 8)
        self.assertEqual(result["disk_gb"], 100)

    # -- _policy_thresholds --------------------------------------------------

    def test_policy_thresholds_balanced(self):
        th = _policy_thresholds("balanced")
        self.assertAlmostEqual(th["scale_out_threshold"], 0.8)
        self.assertAlmostEqual(th["scale_in_threshold"], 0.2)
        self.assertAlmostEqual(th["scale_in_p95_guard"], 0.35)
        self.assertAlmostEqual(th["peak_guard_threshold"], 0.85)

    def test_policy_thresholds_conservative(self):
        th = _policy_thresholds("conservative")
        # Each threshold is shifted -0.05 relative to balanced defaults.
        self.assertAlmostEqual(th["scale_out_threshold"], 0.75)
        self.assertAlmostEqual(th["scale_in_threshold"], 0.15)
        self.assertAlmostEqual(th["scale_in_p95_guard"], 0.30)
        self.assertAlmostEqual(th["peak_guard_threshold"], 0.80)

    def test_policy_thresholds_aggressive(self):
        th = _policy_thresholds("aggressive")
        # Each threshold is shifted +0.05 relative to balanced defaults.
        self.assertAlmostEqual(th["scale_out_threshold"], 0.85)
        self.assertAlmostEqual(th["scale_in_threshold"], 0.25)
        self.assertAlmostEqual(th["scale_in_p95_guard"], 0.40)
        self.assertAlmostEqual(th["peak_guard_threshold"], 0.90)

    # -- _metric_is_hot / _metric_is_cold ------------------------------------

    def test_metric_is_hot_p95(self):
        stats = {"p95": 0.9, "peak": 0.9, "gap": 0.0, "slope": 0.0, "window_mean_delta": 0.0}
        self.assertTrue(_metric_is_hot(stats))

    def test_metric_is_hot_peak_and_gap(self):
        # p95 below threshold, but peak >= peak_guard AND gap >= peak_valley_gap.
        stats = {"p95": 0.7, "peak": 0.85, "gap": 0.35, "slope": 0.0, "window_mean_delta": 0.0}
        self.assertTrue(_metric_is_hot(stats))

    def test_metric_is_hot_uptrend(self):
        # slope and delta both exceed thresholds.
        stats = {"p95": 0.5, "peak": 0.6, "gap": 0.1, "slope": 0.02, "window_mean_delta": 0.1}
        self.assertTrue(_metric_is_hot(stats))

    def test_metric_is_hot_false(self):
        stats = {"p95": 0.5, "peak": 0.6, "gap": 0.1, "slope": 0.0, "window_mean_delta": 0.0}
        self.assertFalse(_metric_is_hot(stats))

    def test_metric_is_cold_true(self):
        stats = {"avg": 0.1, "p95": 0.2}
        self.assertTrue(_metric_is_cold(stats))

    def test_metric_is_cold_false_p95_too_high(self):
        # avg is low but p95 exceeds the p95 guard -> not cold.
        stats = {"avg": 0.1, "p95": 0.5}
        self.assertFalse(_metric_is_cold(stats))

    def test_metric_is_cold_false_avg_too_high(self):
        stats = {"avg": 0.5, "p95": 0.6}
        self.assertFalse(_metric_is_cold(stats))

    # -- _finalize_target_spec_even ------------------------------------------

    def test_finalize_target_spec_even_rounds_odd_up(self):
        target = {"cpu_cores": 5, "memory_gb": 7, "disk_gb": 10}
        result = _finalize_target_spec_even("scale_out", target)
        self.assertEqual(result["cpu_cores"], 6)
        self.assertEqual(result["memory_gb"], 8)
        self.assertEqual(result["disk_gb"], 10)

    def test_finalize_target_spec_even_keeps_even(self):
        target = {"cpu_cores": 6, "memory_gb": 8, "disk_gb": 10}
        result = _finalize_target_spec_even("scale_out", target)
        self.assertEqual(result, target)

    def test_finalize_target_spec_even_disk_shrink_minimum(self):
        target = {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 30}
        result = _finalize_target_spec_even("scale_in", target)
        # 30 is even, but disk_gb under scale_in must be >= 50.
        self.assertEqual(result["disk_gb"], 50)

    def test_finalize_target_spec_even_disk_shrink_odd(self):
        target = {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 49}
        result = _finalize_target_spec_even("scale_in", target)
        # 49 -> 50 (odd snap), then max(50, 50) = 50.
        self.assertEqual(result["disk_gb"], 50)

    def test_finalize_target_spec_hold_no_snap(self):
        target = {"cpu_cores": 5, "memory_gb": 7, "disk_gb": 9}
        result = _finalize_target_spec_even("hold", target)
        # hold action should not modify values.
        self.assertEqual(result, target)

    # -- _max_consecutive ----------------------------------------------------

    def test_max_consecutive_all_high(self):
        values = np.array([0.9, 0.85, 0.9, 0.95])
        result = _max_consecutive(values, lambda x: x >= 0.8)
        self.assertEqual(result, 4)

    def test_max_consecutive_mixed(self):
        values = np.array([0.9, 0.9, 0.5, 0.9, 0.9, 0.9])
        result = _max_consecutive(values, lambda x: x >= 0.8)
        self.assertEqual(result, 3)

    def test_max_consecutive_none_match(self):
        values = np.array([0.1, 0.2, 0.3])
        result = _max_consecutive(values, lambda x: x >= 0.8)
        self.assertEqual(result, 0)

    def test_max_consecutive_empty(self):
        result = _max_consecutive(np.array([]), lambda x: x >= 0.8)
        self.assertEqual(result, 0)

    # -- _trend_features -----------------------------------------------------

    def test_trend_features_uptrend(self):
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        result = _trend_features(values, window=2)
        self.assertGreater(result["slope"], 0.0)
        self.assertGreater(result["window_mean_delta"], 0.0)

    def test_trend_features_downtrend(self):
        values = np.array([8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
        result = _trend_features(values, window=2)
        self.assertLess(result["slope"], 0.0)
        self.assertLess(result["window_mean_delta"], 0.0)

    def test_trend_features_flat(self):
        values = np.array([5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
        result = _trend_features(values, window=2)
        self.assertAlmostEqual(result["slope"], 0.0)
        self.assertAlmostEqual(result["window_mean_delta"], 0.0)

    def test_trend_features_too_short(self):
        result = _trend_features(np.array([1.0]), window=2)
        self.assertAlmostEqual(result["slope"], 0.0)
        self.assertAlmostEqual(result["window_mean_delta"], 0.0)

    # -- _bounded_above / _bounded_below -------------------------------------

    def test_bounded_above_below_threshold(self):
        self.assertAlmostEqual(_bounded_above(0.5, 0.8), 0.0)

    def test_bounded_above_at_threshold(self):
        self.assertAlmostEqual(_bounded_above(0.8, 0.8), 0.0)

    def test_bounded_above_between_threshold_and_cap(self):
        result = _bounded_above(0.9, 0.8)
        self.assertAlmostEqual(result, 0.5)

    def test_bounded_above_at_cap(self):
        result = _bounded_above(1.0, 0.8)
        self.assertAlmostEqual(result, 1.0)

    def test_bounded_above_beyond_cap(self):
        result = _bounded_above(1.5, 0.8)
        self.assertGreater(result, 1.0)

    def test_bounded_below_at_zero(self):
        # (threshold - 0) / threshold = 1.0
        self.assertAlmostEqual(_bounded_below(0.0, 0.2), 1.0)

    def test_bounded_below_at_threshold(self):
        self.assertAlmostEqual(_bounded_below(0.2, 0.2), 0.0)

    def test_bounded_below_above_threshold(self):
        self.assertAlmostEqual(_bounded_below(0.5, 0.2), 0.0)

    def test_bounded_below_halfway(self):
        self.assertAlmostEqual(_bounded_below(0.1, 0.2), 0.5)

    # -- _metric_confidence_score --------------------------------------------

    def test_metric_confidence_score_scale_out_high(self):
        st = {
            "avg": 0.9,
            "p95": 0.95,
            "peak": 0.98,
            "gap": 0.08,
            "high_ratio": 0.9,
            "low_ratio": 0.0,
            "slope": 0.02,
            "window_mean_delta": 0.1,
        }
        score = _metric_confidence_score("scale_out", st)
        self.assertGreaterEqual(score, 70.0)
        self.assertLessEqual(score, 100.0)

    def test_metric_confidence_score_scale_in_high(self):
        st = {
            "avg": 0.05,
            "p95": 0.1,
            "peak": 0.15,
            "gap": 0.1,
            "high_ratio": 0.0,
            "low_ratio": 0.9,
            "slope": -0.02,
            "window_mean_delta": -0.1,
        }
        score = _metric_confidence_score("scale_in", st)
        self.assertGreaterEqual(score, 70.0)
        self.assertLessEqual(score, 100.0)

    def test_metric_confidence_score_hold(self):
        self.assertAlmostEqual(_metric_confidence_score("hold", {}), 50.0)

    # -- _summarize_metric_actions -------------------------------------------

    def test_summarize_metric_actions_all_scale_out(self):
        actions = {"cpu": "scale_out", "memory": "scale_out", "disk": "hold"}
        result = _summarize_metric_actions(actions)
        self.assertEqual(result["action"], "scale_out")
        self.assertEqual(result["suggested_delta"], 1)
        self.assertFalse(result["has_mixed"])

    def test_summarize_metric_actions_mixed_signals(self):
        actions = {"cpu": "scale_out", "memory": "scale_in", "disk": "hold"}
        result = _summarize_metric_actions(actions)
        self.assertEqual(result["action"], "scale_out")
        self.assertTrue(result["has_mixed"])
        self.assertEqual(result["suggested_delta"], 1)
        self.assertIn("cpu", result["out_metrics"])
        self.assertIn("memory", result["in_metrics"])

    def test_summarize_metric_actions_all_scale_in(self):
        actions = {"cpu": "scale_in", "memory": "scale_in", "disk": "scale_in"}
        result = _summarize_metric_actions(actions)
        self.assertEqual(result["action"], "scale_in")
        self.assertEqual(result["suggested_delta"], -1)
        self.assertFalse(result["has_mixed"])

    def test_summarize_metric_actions_all_hold(self):
        actions = {"cpu": "hold", "memory": "hold", "disk": "hold"}
        result = _summarize_metric_actions(actions)
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["suggested_delta"], 0)
        self.assertFalse(result["has_mixed"])

    # -- build_scaling_advice end-to-end -------------------------------------

    def test_build_scaling_advice_high_cpu_triggers_scale_out(self):
        future = _future({"cpu": [0.95] * 24})
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        self.assertEqual(advice["action"], "scale_out")
        self.assertEqual(advice["metric_actions"]["cpu"], "scale_out")
        self.assertIn("cpu", advice["reason"].lower())

    def test_build_scaling_advice_low_usage_triggers_scale_in(self):
        future = _future({
            "cpu": [0.05] * 24,
            "memory": [0.05] * 24,
            "disk": [0.05] * 24,
        })
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        self.assertEqual(advice["action"], "scale_in")
        self.assertEqual(advice["metric_actions"]["cpu"], "scale_in")
        self.assertEqual(advice["metric_actions"]["memory"], "scale_in")

    def test_build_scaling_advice_moderate_usage_triggers_hold(self):
        future = _future({
            "cpu": [0.5] * 24,
            "memory": [0.5] * 24,
            "disk": [0.5] * 24,
        })
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        self.assertEqual(advice["action"], "hold")
        self.assertEqual(advice["suggested_delta"], 0)
        for metric in ("cpu", "memory", "disk"):
            self.assertEqual(advice["metric_actions"][metric], "hold")

    def test_build_scaling_advice_no_current_spec(self):
        future = _future({
            "cpu": [0.95] * 24,
            "memory": [0.5] * 24,
            "disk": [0.3] * 24,
        })
        advice = build_scaling_advice(future, current_spec=None)
        self.assertEqual(advice["action"], "scale_out")
        # Without a valid spec the target cannot be computed.
        self.assertEqual(advice["target_spec"], {})

    def test_build_scaling_advice_missing_disk_metric(self):
        # Only cpu and memory are provided; disk falls back to empty array.
        future = {
            "cpu": np.asarray([0.5] * 24),
            "memory": np.asarray([0.5] * 24),
        }
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        self.assertIn("disk", advice["metric_actions"])
        self.assertIn("disk", advice["stats"])
        self.assertIn(advice["action"], ("hold", "scale_in", "scale_out"))

    def test_build_scaling_advice_target_spec_greater_on_scale_out(self):
        future = _future({"cpu": [0.95] * 24})
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        self.assertEqual(advice["action"], "scale_out")
        self.assertGreaterEqual(advice["target_spec"]["cpu_cores"], VM_SPEC["cpu_cores"])

    def test_build_scaling_advice_target_spec_smaller_on_scale_in(self):
        future = _future({
            "cpu": [0.05] * 24,
            "memory": [0.05] * 24,
            "disk": [0.05] * 24,
        })
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        self.assertEqual(advice["action"], "scale_in")
        self.assertLessEqual(advice["target_spec"]["cpu_cores"], VM_SPEC["cpu_cores"])
        self.assertLessEqual(advice["target_spec"]["memory_gb"], VM_SPEC["memory_gb"])

    def test_build_scaling_advice_cold_with_gentle_uptrend_still_scales_in(self):
        """A cold metric with a gentle uptrend (below hot threshold) still triggers scale_in."""
        # Gentle linear ramp: slope=0.012 but window delta stays below the hot threshold,
        # so _metric_is_hot is False and the metric is cold -> scale_in.
        cpu_values = list(np.linspace(0.05, 0.05 + 0.012 * 11, 12))
        future = _future({
            "cpu": cpu_values,
            "memory": [0.05] * 24,
            "disk": [0.05] * 24,
        })
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        # avg ~0.116, p95 ~0.175 -> cold; slope=0.012 but delta<0.08 -> not hot.
        self.assertEqual(advice["metric_actions"]["cpu"], "scale_in")

    def test_build_scaling_advice_contains_expected_keys(self):
        future = _future({"cpu": [0.5] * 24})
        advice = build_scaling_advice(future, current_spec=VM_SPEC)
        expected_keys = {
            "action",
            "reason",
            "confidence",
            "confidence_score",
            "confidence_metric_scores",
            "policy_tier",
            "risk_profile",
            "action_gate",
            "suggested_delta",
            "metric_actions",
            "metric_reasons",
            "target_spec",
            "stats",
            "has_mixed_signals",
        }
        self.assertEqual(set(advice.keys()), expected_keys)

    def test_build_scaling_advice_with_custom_settings(self):
        """Verify that a custom DecisionConfig override propagates via patch."""
        custom_decision = replace(settings.decision, scale_out_threshold=0.7)
        custom_settings = replace(settings, decision=custom_decision)
        future = _future({"cpu": [0.75] * 24})
        with patch("resource_predict.core.decision.settings", custom_settings):
            advice = build_scaling_advice(future, current_spec=VM_SPEC)
        # With scale_out_threshold=0.7, p95=0.75 >= 0.7 -> hot -> scale_out.
        self.assertEqual(advice["action"], "scale_out")


if __name__ == "__main__":
    unittest.main()
