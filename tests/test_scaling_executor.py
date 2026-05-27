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

    def test_daemonset_plan_skips_replica_command(self):
        resource = {
            "resource_id": "k8s:cluster-k8s-1:cattle-system:daemonset:cattle-node-agent",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster-k8s-1",
                "namespace": "cattle-system",
                "workload_kind": "DaemonSet",
                "workload_name": "cattle-node-agent",
                "containers_observed": ["agent"],
                "replicas": 5,
            },
            "scaling_advice": {
                "action": "scale_in_candidate",
                "target_spec": {
                    "cpu_request_cores": 0.5,
                    "memory_request_gb": 0.25,
                    "replicas": 1,
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

        self.assertEqual(
            plan.commands,
            [
                "kubectl --kubeconfig /root/.kube/config -n cattle-system set resources daemonset/cattle-node-agent "
                "--containers=agent --requests=cpu=500m,memory=256Mi"
            ],
        )
        self.assertTrue(any("DaemonSet replicas" in warning for warning in plan.warnings))

    def test_k8s_manual_plan_generates_per_container_resource_commands(self):
        resource = {
            "resource_id": "k8s:cluster-k8s-1:monitoring:statefulset:alertmanager-main",
            "resource_type": "k8s_workload",
            "spec": {
                "cluster": "cluster-k8s-1",
                "namespace": "monitoring",
                "workload_kind": "StatefulSet",
                "workload_name": "alertmanager-main",
                "containers_observed": ["alertmanager", "config-reloader"],
                "replicas": 2,
            },
            "scaling_advice": {
                "action": "manual",
                "target_spec": {
                    "containers": {
                        "alertmanager": {
                            "memory_request_gb": 0.25,
                            "memory_limit_gb": 0.5,
                        },
                        "config-reloader": {
                            "cpu_request_cores": 0.05,
                            "cpu_limit_cores": 0.1,
                            "memory_limit_gb": 0.025,
                        },
                    },
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

        self.assertEqual(
            plan.commands,
            [
                "kubectl --kubeconfig /root/.kube/config -n monitoring set resources statefulset/alertmanager-main "
                "--containers=alertmanager --requests=memory=256Mi --limits=memory=512Mi",
                "kubectl --kubeconfig /root/.kube/config -n monitoring set resources statefulset/alertmanager-main "
                "--containers=config-reloader --requests=cpu=50m --limits=cpu=100m,memory=26Mi",
                "kubectl --kubeconfig /root/.kube/config -n monitoring scale statefulset/alertmanager-main --replicas=3",
            ],
        )


if __name__ == "__main__":
    unittest.main()
