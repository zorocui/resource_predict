from __future__ import annotations

import unittest

from resource_predict.services.urgency import compute_urgency_score
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


if __name__ == "__main__":
    unittest.main()
