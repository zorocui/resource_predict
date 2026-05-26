from __future__ import annotations

import unittest

import numpy as np

from resource_predict.core.k8s_workload_decision import build_k8s_workload_advice


class K8SWorkloadDecisionTest(unittest.TestCase):
    def test_missing_request_limit_is_trend_only_not_insufficient(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
            },
            "data_quality": {
                "cpu": {"level": "good"},
                "memory": {"level": "good"},
            },
        }

        advice = build_k8s_workload_advice(
            {
                "cpu": np.array([0.9, 0.95, 1.0]),
                "memory": np.array([0.5, 0.55, 0.6]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "scale_out_candidate")
        self.assertEqual(advice["metric_actions"]["cpu"], "scale_out_candidate")
        self.assertEqual(advice["metric_actions"]["memory"], "hold")
        self.assertNotIn("insufficient_data", advice["metric_actions"].values())
        self.assertIn("trend only", advice["metric_reasons"]["cpu"])
        self.assertFalse(advice["target_k8s_policy"]["ready_for_execution"])

    def test_poor_quality_remains_insufficient(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {"cpu_request_cores": 1.0, "memory_request_gb": 1.0},
            "data_quality": {
                "cpu": {"level": "poor"},
                "memory": {"level": "poor"},
            },
        }

        advice = build_k8s_workload_advice(
            {"cpu": np.array([0.9, 0.95]), "memory": np.array([0.8, 0.85])},
            resource=resource,
        )

        self.assertEqual(advice["action"], "insufficient_data")
        self.assertEqual(advice["metric_actions"]["cpu"], "insufficient_data")
        self.assertEqual(advice["metric_actions"]["memory"], "insufficient_data")

    def test_target_spec_includes_requests_limits_and_replicas(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "replicas_observed": 2,
                "cpu_request_cores": 0.5,
                "cpu_limit_cores": 1.0,
                "memory_request_gb": 1.0,
                "memory_limit_gb": 2.0,
            },
            "data_quality": {
                "cpu": {"level": "good"},
                "memory": {"level": "good"},
            },
        }

        advice = build_k8s_workload_advice(
            {
                "cpu": np.array([0.92, 0.95, 0.98]),
                "memory": np.array([0.82, 0.86, 0.9]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "scale_out_candidate")
        self.assertFalse(advice["analysis_only"])
        self.assertGreater(advice["target_spec"]["cpu_request_cores"], 0.5)
        self.assertGreater(advice["target_spec"]["memory_request_gb"], 1.0)
        self.assertGreater(advice["target_spec"]["replicas"], 2)
        self.assertEqual(
            advice["target_k8s_policy"]["recommendations"]["replicas"]["target_replicas"],
            advice["target_spec"]["replicas"],
        )


if __name__ == "__main__":
    unittest.main()
