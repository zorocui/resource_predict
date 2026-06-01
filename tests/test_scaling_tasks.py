from __future__ import annotations

import unittest

from resource_predict.services.scaling.tasks import (
    _execution_gate_failures,
    _resource_with_target_override,
    create_scaling_task,
)


K8S_QUALITY_GOOD = {
    "cpu_limit": {"level": "good"},
    "cpu_request": {"level": "good"},
    "memory_limit": {"level": "good"},
    "memory_request": {"level": "good"},
}


class ScalingTasksTest(unittest.TestCase):
    def test_manual_target_override_preserves_resource_and_replaces_task_advice_target(self):
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {"cluster": "cluster", "namespace": "ns"},
            "scaling_advice": {
                "action": "hold",
                "target_spec": {"replicas": 2},
            },
        }
        manual_target = {
            "containers": {
                "app": {"cpu_request_cores": 0.5},
            },
            "replicas": 3,
        }

        patched = _resource_with_target_override(resource, manual_target, "manual")

        self.assertIsNot(patched, resource)
        self.assertEqual(resource["scaling_advice"]["target_spec"], {"replicas": 2})
        self.assertEqual(patched["scaling_advice"]["action"], "manual")
        self.assertEqual(patched["scaling_advice"]["target_source"], "manual")
        self.assertEqual(patched["scaling_advice"]["target_spec"], manual_target)

    def test_execute_rejects_observe_action_gate_before_queueing(self):
        resource = {
            "resource_id": "vm-gate-observe-test",
            "resource_type": "openstack_vm",
            "spec": {"cluster": "cluster-a", "cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
            "scaling_advice": {
                "action": "scale_out",
                "confidence": "high",
                "confidence_score": 90.0,
                "policy_tier": "balanced",
                "action_gate": {"state": "observe"},
                "risk_profile": {"cooldown_minutes": 60},
                "target_spec": {"cpu_cores": 8, "memory_gb": 8, "disk_gb": 100},
            },
        }

        with self.assertRaisesRegex(RuntimeError, "action_gate"):
            create_scaling_task(resource, mode="execute")

    def test_execution_gate_checks_data_quality_and_cooldown(self):
        now_ms = 1_780_000_000_000
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "last_scaled_at_epoch_ms": now_ms - 10 * 60_000,
            },
            "data_quality": {
                **K8S_QUALITY_GOOD,
                "cpu_limit": {"level": "fair"},
            },
            "scaling_advice": {
                "action": "scale_out_candidate",
                "confidence": "high",
                "confidence_score": 88.0,
                "policy_tier": "balanced",
                "action_gate": {"state": "ready"},
                "metric_actions": {"cpu": "scale_out_candidate", "memory": "hold"},
                "risk_profile": {"cooldown_minutes": 60},
                "target_k8s_policy": {"ready_for_execution": True},
                "target_spec": {"cpu_request_cores": 0.5},
            },
        }

        failures = _execution_gate_failures(resource, now_ms=now_ms)

        self.assertTrue(any("data_quality" in item for item in failures))
        self.assertTrue(any("cooldown" in item for item in failures))

    def test_execution_gate_accepts_ready_high_confidence_good_quality(self):
        now_ms = 1_780_000_000_000
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster",
                "namespace": "ns",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "last_scaled_at_epoch_ms": now_ms - 2 * 60 * 60_000,
            },
            "data_quality": K8S_QUALITY_GOOD,
            "scaling_advice": {
                "action": "scale_out_candidate",
                "confidence": "high",
                "confidence_score": 88.0,
                "policy_tier": "balanced",
                "action_gate": {"state": "ready"},
                "metric_actions": {"cpu": "scale_out_candidate", "memory": "hold"},
                "risk_profile": {"cooldown_minutes": 60},
                "target_k8s_policy": {"ready_for_execution": True},
                "target_spec": {"cpu_request_cores": 0.5},
            },
        }

        self.assertEqual(_execution_gate_failures(resource, now_ms=now_ms), [])


if __name__ == "__main__":
    unittest.main()
