from __future__ import annotations

import unittest

import numpy as np

from resource_predict.core.k8s_workload_decision import build_k8s_workload_advice


def _containers(
    *,
    cpu_request=None,
    cpu_limit=None,
    memory_request=None,
    memory_limit=None,
) -> dict:
    return {
        "containers": {
            "app": {
                "cpu_request_cores": cpu_request,
                "cpu_limit_cores": cpu_limit,
                "memory_request_gb": memory_request,
                "memory_limit_gb": memory_limit,
            }
        }
    }


def _quality(level: str = "good") -> dict:
    return {
        "cpu_limit": {"level": level},
        "cpu_request": {"level": level},
        "memory_limit": {"level": level},
        "memory_request": {"level": level},
    }


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

        # 没有 request/limit baseline 时，不应给出扩缩容建议（无法计算利用率）
        self.assertEqual(advice["action"], "hold")
        self.assertEqual(advice["metric_actions"]["cpu"], "hold")
        self.assertEqual(advice["metric_actions"]["memory"], "hold")
        self.assertIn("lacks request/limit baseline", advice["metric_reasons"]["cpu"])
        self.assertIn("lacks request/limit baseline", advice["metric_reasons"]["memory"])
        self.assertEqual(advice["target_spec"], {})
        self.assertFalse(advice["target_k8s_policy"]["ready_for_execution"])

    def test_poor_quality_remains_insufficient(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": _containers(cpu_request=1.0, memory_request=1.0),
            "data_quality": _quality("poor"),
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
                **_containers(cpu_request=2.0, cpu_limit=4.0, memory_request=2.0, memory_limit=4.0),
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
        self.assertGreaterEqual(advice["target_spec"]["cpu_request_cores"], 2.0)
        self.assertGreaterEqual(advice["target_spec"]["memory_request_gb"], 2.0)
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

    def test_scale_out_preserves_small_request_limit_granularity(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "replicas_observed": 2,
                **_containers(cpu_request=0.5, cpu_limit=0.5, memory_request=0.5, memory_limit=0.5),
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
        self.assertLess(advice["target_spec"]["cpu_request_cores"], 2.0)
        self.assertLess(advice["target_spec"]["cpu_limit_cores"], 2.0)
        self.assertLess(advice["target_spec"]["memory_request_gb"], 2.0)
        self.assertLess(advice["target_spec"]["memory_limit_gb"], 2.0)
        self.assertEqual(advice["target_spec"]["cpu_request_cores"], 0.5)
        self.assertEqual(advice["target_spec"]["memory_request_gb"], 0.5)
        self.assertGreater(advice["target_spec"]["replicas"], 2)

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
                **_containers(cpu_request=1.0, cpu_limit=2.0, memory_request=1.0, memory_limit=2.0),
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
                **_containers(cpu_request=1.0, cpu_limit=1.0, memory_request=1.0, memory_limit=1.0),
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

    def test_request_based_high_usage_does_not_recommend_scale_out(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "replicas_observed": 2,
                **_containers(cpu_request=0.1, memory_request=0.1),
                "cpu_metric_mode": "cpu_usage/cpu_request",
                "memory_metric_mode": "memory_working_set/memory_request",
            },
            "data_quality": {
                "cpu": {"level": "good"},
                "memory": {"level": "good"},
            },
        }

        advice = build_k8s_workload_advice(
            {
                "cpu": np.array([8.0, 10.0, 12.0]),
                "memory": np.array([9.0, 11.0, 13.0]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "hold")
        self.assertEqual(advice["metric_actions"]["cpu"], "hold")
        self.assertEqual(advice["metric_actions"]["memory"], "hold")
        self.assertIn("no limit baseline for scale-out", advice["metric_reasons"]["cpu"])
        self.assertEqual(advice["target_spec"], {})

    def test_request_based_low_usage_can_recommend_scale_in(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "replicas_observed": 3,
                **_containers(cpu_request=4.0, memory_request=4.0),
                "cpu_metric_mode": "cpu_usage/cpu_request",
                "memory_metric_mode": "memory_working_set/memory_request",
            },
            "data_quality": {
                "cpu": {"level": "good"},
                "memory": {"level": "good"},
            },
        }

        advice = build_k8s_workload_advice(
            {
                "cpu": np.array([0.02, 0.03, 0.04]),
                "memory": np.array([0.03, 0.04, 0.05]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "scale_in_candidate")
        self.assertLess(advice["target_spec"]["cpu_request_cores"], 4.0)
        self.assertLess(advice["target_spec"]["memory_request_gb"], 4.0)

    def test_cpu_idle_but_memory_busy_reduces_cpu_without_scaling_replicas(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "replicas_observed": 6,
                **_containers(cpu_request=4.0, cpu_limit=4.0, memory_request=4.0, memory_limit=4.0),
            },
            "data_quality": _quality("good"),
        }

        advice = build_k8s_workload_advice(
            {
                "cpu_request": np.array([0.01, 0.01, 0.01]),
                "cpu_limit": np.array([0.01, 0.01, 0.01]),
                "memory_request": np.array([0.75, 0.75, 0.75]),
                "memory_limit": np.array([0.75, 0.75, 0.75]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "scale_in_candidate")
        self.assertEqual(advice["metric_actions"]["cpu"], "scale_in_candidate")
        self.assertEqual(advice["metric_actions"]["memory"], "hold")
        self.assertLess(advice["target_spec"]["cpu_request_cores"], 4.0)
        self.assertIn("cpu_limit_cores", advice["target_spec"])
        self.assertNotIn("replicas", advice["target_spec"])
        self.assertTrue(
            any("replica scale-in requires both CPU and memory to be low" in note for note in advice["target_k8s_policy"]["notes"])
        )

    def test_replica_scale_in_is_capped_to_one_step_reduction(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "replicas_observed": 6,
                **_containers(cpu_request=4.0, memory_request=4.0),
            },
            "data_quality": _quality("good"),
        }

        advice = build_k8s_workload_advice(
            {
                "cpu_request": np.array([0.01, 0.01, 0.01]),
                "memory_request": np.array([0.01, 0.01, 0.01]),
            },
            resource=resource,
        )

        self.assertEqual(advice["action"], "scale_in_candidate")
        self.assertEqual(advice["target_spec"]["replicas"], 3)

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
                **_containers(cpu_request=8.0, cpu_limit=10.0, memory_request=8.0, memory_limit=10.0),
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
