from __future__ import annotations

import unittest

from resource_predict.services.scaling.executor import build_scaling_plan


class ScalingExecutorTest(unittest.TestCase):
    def test_k8s_workload_plan_generates_resource_and_replica_commands(self):
        resource = {
            "resource_id": "k8s:cluster-k8s-a:payments:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster-k8s-a",
                "namespace": "payments",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "containers_observed": ["app"],
                "replicas_observed": 2,
            },
            "scaling_advice": {
                "action": "scale_out_candidate",
                "target_spec": {
                    "cpu_request_cores": 0.75,
                    "cpu_limit_cores": 1.0,
                    "memory_request_gb": 1.5,
                    "memory_limit_gb": 2.0,
                    "replicas": 3,
                },
            },
        }

        plan = build_scaling_plan(
            resource,
            {
                "cloud_type": "k8s",
                "control_host": "10.0.0.10",
                "ssh_user": "root",
                "kubeconfig": "/root/.kube/config",
            },
        )

        self.assertEqual(plan.resource_type, "k8s_workload")
        self.assertEqual(plan.cluster, "cluster-k8s-a")
        self.assertEqual(
            plan.commands,
            [
                "kubectl --kubeconfig /root/.kube/config -n payments set resources deployment/api "
                "--containers=app --requests=cpu=750m,memory=1536Mi --limits=cpu=1,memory=2Gi",
                "kubectl --kubeconfig /root/.kube/config -n payments scale deployment/api --replicas=3",
            ],
        )
        self.assertEqual(plan.target_spec["replicas"], 3)


if __name__ == "__main__":
    unittest.main()
