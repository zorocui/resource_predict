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
        for key in (
            "cpu_request_cores",
            "cpu_limit_cores",
            "memory_request_gb",
            "memory_limit_gb",
        ):
            self.assertIsInstance(advice["target_spec"][key], int)
            self.assertEqual(advice["target_spec"][key] % 2, 0)
        self.assertGreater(advice["target_spec"]["replicas"], 2)
        self.assertEqual(
            advice["target_k8s_policy"]["recommendations"]["replicas"]["target_replicas"],
            advice["target_spec"]["replicas"],
        )

    def test_scale_in_does_not_round_small_specs_up_into_resource_expansion(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "replicas_observed": 3,
                "cpu_request_cores": 1.0,
                "cpu_limit_cores": 2.0,
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
                "cpu": np.array([0.05, 0.08, 0.1]),
                "memory": np.array([0.05, 0.08, 0.1]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "scale_in_candidate")
        self.assertNotIn("cpu_request_cores", advice["target_spec"])
        self.assertNotIn("memory_request_gb", advice["target_spec"])
        self.assertLess(advice["target_spec"]["replicas"], 3)

    def test_replica_policy_prefers_controller_replicas_over_observed_pods(self):
        resource = {
            "resource_id": "k8s:cluster:ns:statefulset:api",
            "resource_type": "k8s_workload",
            "spec": {
                "workload_kind": "StatefulSet",
                "replicas": 3,
                "replicas_observed": 2,
                "cpu_request_cores": 1.0,
                "memory_request_gb": 1.0,
            },
            "data_quality": {
                "cpu": {"level": "good"},
                "memory": {"level": "good"},
            },
        }

        advice = build_k8s_workload_advice(
            {
                "cpu": np.array([0.92, 0.95, 0.98]),
                "memory": np.array([0.2, 0.25, 0.3]),
            },
            resource=resource,
        )

        self.assertEqual(
            advice["target_k8s_policy"]["recommendations"]["replicas"]["current_replicas"],
            3,
        )

    def test_daemonset_policy_does_not_recommend_replica_scaling(self):
        resource = {
            "resource_id": "k8s:cluster:cattle-system:daemonset:cattle-node-agent",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster-k8s-1",
                "namespace": "cattle-system",
                "workload_kind": "DaemonSet",
                "workload_name": "cattle-node-agent",
                "replicas": 6,
                "replicas_observed": 6,
                "cpu_request_cores": 8.0,
                "cpu_limit_cores": 10.0,
                "memory_request_gb": 8.0,
                "memory_limit_gb": 10.0,
            },
            "data_quality": {
                "cpu": {"level": "good"},
                "memory": {"level": "good"},
            },
        }

        advice = build_k8s_workload_advice(
            {
                "cpu": np.array([0.05, 0.08, 0.1]),
                "memory": np.array([0.06, 0.09, 0.12]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "scale_in_candidate")
        self.assertIn("cpu_request_cores", advice["target_spec"])
        self.assertIn("memory_request_gb", advice["target_spec"])
        self.assertNotIn("replicas", advice["target_spec"])
        self.assertNotIn("replicas", advice["target_k8s_policy"]["recommendations"])
        self.assertTrue(
            any("DaemonSet replicas follow node scheduling" in note for note in advice["target_k8s_policy"]["notes"])
        )


if __name__ == "__main__":
    unittest.main()
