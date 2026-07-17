from __future__ import annotations

import copy
import unittest

from resource_predict.services.urgency import compute_urgency_breakdown, compute_urgency_score
from resource_predict.settings import settings


class UrgencyScoreTest(unittest.TestCase):
    def _k8s_scale_in_item(self, *, analysis_only: bool, ready_for_execution: bool, target_spec: dict) -> dict:
        return {
            "resource_id": "k8s:cluster:kube-system:deployment:descheduler",
            "resource_type": "k8s_workload",
            "spec": {"replicas": 1},
            "scaling_advice": {
                "action": "scale_in_candidate",
                "analysis_only": analysis_only,
                "confidence": "high",
                "metric_actions": {"cpu": "scale_in_candidate", "memory": "scale_in_candidate"},
                "risk_profile": {"risk_score": 99.0},
                "stats": {
                    "cpu": {"avg": 0.001, "p95": 0.001, "peak": 0.001, "gap": 0.0},
                    "memory": {"avg": 0.145, "p95": 0.145, "peak": 0.145, "gap": 0.0},
                },
                "target_k8s_policy": {"ready_for_execution": ready_for_execution},
                "target_spec": target_spec,
            },
        }

    def test_k8s_analysis_only_scale_in_is_capped_to_low_priority(self):
        item = self._k8s_scale_in_item(
            analysis_only=True,
            ready_for_execution=False,
            target_spec={},
        )

        self.assertLessEqual(compute_urgency_score(item, settings.decision), 25.0)

    def test_k8s_executable_candidate_keeps_full_urgency(self):
        executable = self._k8s_scale_in_item(
            analysis_only=False,
            ready_for_execution=True,
            target_spec={"replicas": 1},
        )
        analysis_only = self._k8s_scale_in_item(
            analysis_only=True,
            ready_for_execution=False,
            target_spec={},
        )

        self.assertGreater(
            compute_urgency_score(executable, settings.decision),
            compute_urgency_score(analysis_only, settings.decision),
        )

    def test_breakdown_score_matches_urgency_score(self):
        item = self._k8s_scale_in_item(
            analysis_only=False,
            ready_for_execution=True,
            target_spec={"replicas": 1},
        )

        breakdown = compute_urgency_breakdown(item, settings.decision)

        self.assertEqual(breakdown["score"], compute_urgency_score(item, settings.decision))
        self.assertTrue(breakdown["components"])
        self.assertTrue(breakdown["metric_scores"])

    def test_k8s_residual_disk_data_does_not_affect_urgency(self):
        clean = self._k8s_scale_in_item(
            analysis_only=False,
            ready_for_execution=True,
            target_spec={"replicas": 1},
        )
        dirty = copy.deepcopy(clean)
        dirty["spec"]["disk_gb"] = 100
        dirty["scaling_advice"]["target_spec"]["disk_gb"] = 50
        dirty["scaling_advice"]["metric_actions"]["disk"] = "scale_in_candidate"
        dirty["scaling_advice"]["stats"]["disk"] = {
            "avg": 0.01,
            "p95": 0.01,
            "peak": 0.01,
            "gap": 0.0,
        }

        clean_breakdown = compute_urgency_breakdown(clean, settings.decision)
        dirty_breakdown = compute_urgency_breakdown(dirty, settings.decision)

        self.assertEqual(dirty_breakdown["score"], clean_breakdown["score"])
        self.assertEqual(
            [entry["metric"] for entry in dirty_breakdown["metric_scores"]],
            ["cpu", "memory"],
        )

    def test_vm_disk_signal_still_contributes_to_urgency(self):
        item = {
            "resource_id": "vm:cluster-a:server-1",
            "resource_type": "openstack_vm",
            "spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
            "scaling_advice": {
                "action": "scale_out",
                "confidence": "high",
                "metric_actions": {"disk": "scale_out"},
                "risk_profile": {"risk_score": 80.0},
                "stats": {
                    "disk": {
                        "avg": 0.9,
                        "p95": 0.95,
                        "peak": 0.98,
                        "gap": 0.08,
                    }
                },
                "target_spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 150},
            },
        }

        breakdown = compute_urgency_breakdown(item, settings.decision)

        metric_scores = {entry["metric"]: entry["value"] for entry in breakdown["metric_scores"]}
        self.assertIn("disk", metric_scores)
        self.assertGreater(metric_scores["disk"], 0.0)


if __name__ == "__main__":
    unittest.main()
