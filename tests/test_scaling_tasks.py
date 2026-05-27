from __future__ import annotations

import unittest

from resource_predict.services.scaling.tasks import _resource_with_target_override


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


if __name__ == "__main__":
    unittest.main()
