from __future__ import annotations

import unittest
from unittest.mock import patch

from resource_predict.providers import k8s_prometheus as provider
from resource_predict.providers.k8s_prometheus import PrometheusTarget


BASE_TS = 1_700_000_000
GIB = 1024 ** 3


class FakePrometheusClient:
    def __init__(self, *args, **kwargs):
        pass

    def query_range(self, query: str, *, start: float, end: float, step: int):
        if "container_cpu_usage_seconds_total" in query:
            return [
                self._range_row("ns", "api-rs-a", "app", "node-1", [0.2, 0.4]),
                self._range_row("ns", "api-rs-b", "sidecar", "node-2", [0.1, 0.2]),
                self._range_row("ns", "orphan", "app", "node-3", [9.0, 9.0]),
            ]
        if "container_memory_working_set_bytes" in query:
            return [
                self._range_row("ns", "api-rs-a", "app", "node-1", [0.5 * GIB, 0.6 * GIB]),
                self._range_row("ns", "api-rs-b", "sidecar", "node-2", [0.5 * GIB, 0.8 * GIB]),
                self._range_row("ns", "orphan", "app", "node-3", [9.0 * GIB, 9.0 * GIB]),
            ]
        return []

    def query(self, query: str, *, ts=None):
        if "kube_pod_owner" in query:
            return [
                self._instant_row(
                    {"namespace": "ns", "pod": "api-rs-a", "owner_kind": "ReplicaSet", "owner_name": "api-rs"},
                    1,
                ),
                self._instant_row(
                    {"namespace": "ns", "pod": "api-rs-b", "owner_kind": "ReplicaSet", "owner_name": "api-rs"},
                    1,
                ),
            ]
        if "kube_replicaset_owner" in query:
            return [
                self._instant_row(
                    {
                        "namespace": "ns",
                        "replicaset": "api-rs",
                        "owner_kind": "Deployment",
                        "owner_name": "api",
                    },
                    1,
                )
            ]
        if "requests_cpu_cores" in query or 'resource="cpu"' in query and "requests" in query:
            return [
                self._resource_row("ns", "api-rs-a", "app", 1.0),
                self._resource_row("ns", "api-rs-b", "sidecar", 1.0),
                self._resource_row("ns", "orphan", "app", 10.0),
            ]
        if "limits_cpu_cores" in query or 'resource="cpu"' in query and "limits" in query:
            return []
        if "requests_memory_bytes" in query or 'resource="memory"' in query and "requests" in query:
            return []
        if "limits_memory_bytes" in query or 'resource="memory"' in query and "limits" in query:
            return [
                self._resource_row("ns", "api-rs-a", "app", 1.0 * GIB),
                self._resource_row("ns", "api-rs-b", "sidecar", 1.0 * GIB),
                self._resource_row("ns", "orphan", "app", 10.0 * GIB),
            ]
        return []

    @staticmethod
    def _range_row(namespace: str, pod: str, container: str, node: str, values: list[float]) -> dict:
        return {
            "metric": {"namespace": namespace, "pod": pod, "container": container, "node": node},
            "values": [[BASE_TS + i * 300, str(value)] for i, value in enumerate(values)],
        }

    @staticmethod
    def _resource_row(namespace: str, pod: str, container: str, value: float) -> dict:
        return FakePrometheusClient._instant_row(
            {"namespace": namespace, "pod": pod, "container": container},
            value,
        )

    @staticmethod
    def _instant_row(metric: dict, value: float) -> dict:
        return {"metric": metric, "value": [BASE_TS, str(value)]}


class K8SWorkloadProviderTest(unittest.TestCase):
    def test_fetch_target_aggregates_pods_to_deployment_workload(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            items = provider._fetch_target(target, limit=0)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["resource_id"], "k8s:cluster-a:ns:deployment:api")
        self.assertEqual(item["resource_type"], "k8s_workload")
        self.assertEqual(item["spec"]["workload_kind"], "Deployment")
        self.assertEqual(item["spec"]["workload_name"], "api")
        self.assertEqual(item["spec"]["pods_observed"], ["api-rs-a", "api-rs-b"])
        self.assertEqual(item["spec"]["containers_observed"], ["app", "sidecar"])
        self.assertEqual(item["spec"]["replicas_observed"], 2)
        self.assertEqual(item["spec"]["nodes"], ["node-1", "node-2"])
        self.assertEqual(item["spec"]["cpu_request_cores"], 2.0)
        self.assertEqual(item["spec"]["memory_limit_gb"], 2.0)
        self.assertEqual(item["spec"]["cpu_metric_mode"], "cpu_usage/cpu_request")
        self.assertEqual(item["spec"]["memory_metric_mode"], "memory_working_set/memory_limit")
        self.assertEqual(len(item["metrics"]["cpu"]["values"]), 2)
        self.assertAlmostEqual(item["metrics"]["cpu"]["values"][0], 0.15)
        self.assertAlmostEqual(item["metrics"]["cpu"]["values"][1], 0.3)
        self.assertEqual(len(item["metrics"]["memory"]["values"]), 2)
        self.assertAlmostEqual(item["metrics"]["memory"]["values"][0], 0.5)
        self.assertAlmostEqual(item["metrics"]["memory"]["values"][1], 0.7)

    def test_diagnose_target_reports_workload_readiness(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            report = provider._diagnose_target(target)

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["workloads_resolved"], 1)
        self.assertEqual(report["counts"]["orphan_container_series"], 1)
        self.assertEqual(report["sample_workloads"], [
            {"namespace": "ns", "workload_kind": "Deployment", "workload_name": "api"}
        ])
        self.assertTrue(any("缺少 owner" in warning for warning in report["warnings"]))
        self.assertEqual(report["errors"], [])


if __name__ == "__main__":
    unittest.main()
