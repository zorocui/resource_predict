from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from resource_predict.utils import (
    compute_metric_stats,
    parse_float_or_none,
    parse_positive_finite,
    parse_positive_int,
    require_float,
    resolve_policy_tier,
)


class ParsePositiveFiniteTest(unittest.TestCase):
    """Tests for parse_positive_finite."""

    def test_valid_positive(self):
        self.assertEqual(parse_positive_finite(3.14), 3.14)
        self.assertEqual(parse_positive_finite("2.5"), 2.5)
        self.assertEqual(parse_positive_finite(1), 1.0)

    def test_zero_returns_none(self):
        self.assertIsNone(parse_positive_finite(0))
        self.assertIsNone(parse_positive_finite("0"))

    def test_negative_returns_none(self):
        self.assertIsNone(parse_positive_finite(-1.0))

    def test_nan_returns_none(self):
        self.assertIsNone(parse_positive_finite(float("nan")))

    def test_inf_returns_none(self):
        self.assertIsNone(parse_positive_finite(float("inf")))
        self.assertIsNone(parse_positive_finite(float("-inf")))

    def test_none_returns_none(self):
        self.assertIsNone(parse_positive_finite(None))

    def test_non_numeric_string_returns_none(self):
        self.assertIsNone(parse_positive_finite("abc"))


class ParseFloatOrNoneTest(unittest.TestCase):
    """Tests for parse_float_or_none."""

    def test_valid_values(self):
        self.assertEqual(parse_float_or_none(3.14), 3.14)
        self.assertEqual(parse_float_or_none("2.7"), 2.7)
        self.assertEqual(parse_float_or_none(0), 0.0)
        self.assertEqual(parse_float_or_none(-1.5), -1.5)

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_float_or_none("abc"))
        self.assertIsNone(parse_float_or_none(None))
        self.assertIsNone(parse_float_or_none([]))


class ParsePositiveIntTest(unittest.TestCase):
    """Tests for parse_positive_int."""

    def test_valid_positive(self):
        self.assertEqual(parse_positive_int(5), 5)
        self.assertEqual(parse_positive_int("10"), 10)
        self.assertEqual(parse_positive_int(3.7), 3)  # int(float(3.7)) = 3

    def test_negative_returns_default(self):
        self.assertIsNone(parse_positive_int(-1))
        self.assertEqual(parse_positive_int(-1, default=42), 42)

    def test_zero_returns_default(self):
        self.assertIsNone(parse_positive_int(0))

    def test_invalid_returns_default(self):
        self.assertIsNone(parse_positive_int("abc"))
        self.assertEqual(parse_positive_int("abc", default=7), 7)

    def test_default_none(self):
        self.assertIsNone(parse_positive_int(None))


class ResolvePolicyTierTest(unittest.TestCase):
    """Tests for resolve_policy_tier."""

    def _make_cfg(self, **overrides):
        defaults = {
            "default_policy_tier": "balanced",
            "conservative_namespaces": ("prod", "production", "payments"),
            "aggressive_namespaces": ("dev", "test", "staging"),
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_explicit_tier_in_spec(self):
        """Explicit policy_tier field takes precedence."""
        cfg = self._make_cfg()
        self.assertEqual(
            resolve_policy_tier({"policy_tier": "aggressive"}, fields=["namespace"], cfg=cfg),
            "aggressive",
        )
        self.assertEqual(
            resolve_policy_tier({"policy_tier": "Conservative"}, fields=["namespace"], cfg=cfg),
            "conservative",
        )

    def test_namespace_matches_conservative(self):
        """Namespace containing a conservative keyword resolves to conservative."""
        cfg = self._make_cfg()
        result = resolve_policy_tier(
            {"namespace": "prod-us-east"}, fields=["namespace"], cfg=cfg
        )
        self.assertEqual(result, "conservative")

    def test_namespace_matches_aggressive(self):
        """Namespace containing an aggressive keyword resolves to aggressive."""
        cfg = self._make_cfg()
        result = resolve_policy_tier(
            {"namespace": "dev-team-a"}, fields=["namespace"], cfg=cfg
        )
        self.assertEqual(result, "aggressive")

    def test_default_tier_fallback(self):
        """No match falls back to the configured default tier."""
        cfg = self._make_cfg(default_policy_tier="balanced")
        result = resolve_policy_tier(
            {"namespace": "unknown-ns"}, fields=["namespace"], cfg=cfg
        )
        self.assertEqual(result, "balanced")

    def test_empty_spec_uses_default(self):
        """Empty spec resolves to default tier."""
        cfg = self._make_cfg()
        result = resolve_policy_tier({}, fields=["namespace"], cfg=cfg)
        self.assertEqual(result, "balanced")

    def test_invalid_default_falls_back_to_balanced(self):
        """If default_policy_tier is invalid, falls back to 'balanced'."""
        cfg = self._make_cfg(default_policy_tier="invalid_tier")
        result = resolve_policy_tier({}, fields=["namespace"], cfg=cfg)
        self.assertEqual(result, "balanced")


class ComputeMetricStatsTest(unittest.TestCase):
    """Tests for compute_metric_stats."""

    def test_basic_stats(self):
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = compute_metric_stats(values)
        self.assertAlmostEqual(result["avg"], 3.0)
        self.assertAlmostEqual(result["peak"], 5.0)
        self.assertIn("p95", result)
        # basic mode should NOT include extended keys
        self.assertNotIn("valley", result)
        self.assertNotIn("gap", result)
        self.assertNotIn("std", result)

    def test_empty_array(self):
        result = compute_metric_stats(np.array([]))
        self.assertEqual(result["avg"], 0.0)
        self.assertEqual(result["p95"], 0.0)
        self.assertEqual(result["peak"], 0.0)

    def test_extended_mode(self):
        values = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        result = compute_metric_stats(values, extended=True)
        self.assertAlmostEqual(result["valley"], 2.0)
        self.assertAlmostEqual(result["gap"], 8.0)  # 10 - 2
        self.assertIn("std", result)
        self.assertGreater(result["std"], 0)

    def test_filter_nonfinite_true(self):
        """Non-finite values are excluded when filter_nonfinite=True (default)."""
        values = np.array([1.0, 2.0, float("inf"), float("nan"), 3.0])
        result = compute_metric_stats(values)
        self.assertAlmostEqual(result["peak"], 3.0)
        self.assertAlmostEqual(result["avg"], 2.0)

    def test_filter_nonfinite_false(self):
        """Non-finite values propagate when filter_nonfinite=False."""
        values = np.array([1.0, 2.0, float("inf")])
        result = compute_metric_stats(values, filter_nonfinite=False)
        self.assertEqual(result["peak"], float("inf"))

    def test_empty_after_filtering(self):
        """All non-finite values with filter_nonfinite=True yields zero stats."""
        values = np.array([float("inf"), float("nan")])
        result = compute_metric_stats(values, extended=True)
        self.assertEqual(result["avg"], 0.0)
        self.assertEqual(result["valley"], 0.0)
        self.assertEqual(result["std"], 0.0)


class RequireFloatTest(unittest.TestCase):
    """Tests for require_float."""

    def test_success(self):
        self.assertEqual(require_float(3.14, "threshold"), 3.14)
        self.assertEqual(require_float("2.5", "rate"), 2.5)

    def test_raises_value_error_on_invalid(self):
        with self.assertRaises(ValueError) as ctx:
            require_float("abc", "threshold")
        self.assertIn("threshold", str(ctx.exception))

    def test_raises_custom_error_class(self):
        class AppError(Exception):
            pass

        with self.assertRaises(AppError):
            require_float(None, "weight", error_cls=AppError)


if __name__ == "__main__":
    unittest.main()
