from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resource_predict.providers import k8s_prometheus as provider
from resource_predict.providers.k8s_prometheus import PrometheusTarget


BASE_TS = 1_700_000_000
GIB = 1024 ** 3


class FakePrometheusClient:
    queries: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def query_range(self, query: str, *, start: float, end: float, step: int):
        self.queries.append(query)
        if "container_cpu_usage_seconds_total" in query:
            rows = [
                self._range_row("ns", "api-rs-a", "app", "node-1", [0.2, 0.4]),
                self._range_row("ns", "api-rs-b", "sidecar", "node-2", [0.1, 0.2]),
                self._range_row("ns", "orphan", "app", "node-3", [9.0, 9.0]),
            ]
            if self._includes_pod_container(query):
                rows.append(self._range_row("ns", "api-rs-a", "POD", "node-1", [5.0, 5.0]))
            return rows
        if "container_memory_working_set_bytes" in query:
            rows = [
                self._range_row("ns", "api-rs-a", "app", "node-1", [0.5 * GIB, 0.6 * GIB]),
                self._range_row("ns", "api-rs-b", "sidecar", "node-2", [0.5 * GIB, 0.8 * GIB]),
                self._range_row("ns", "orphan", "app", "node-3", [9.0 * GIB, 9.0 * GIB]),
            ]
            if self._includes_pod_container(query):
                rows.append(self._range_row("ns", "api-rs-a", "POD", "node-1", [5.0 * GIB, 5.0 * GIB]))
            return rows
        return []

    def query(self, query: str, *, ts=None):
        self.queries.append(query)
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
        if "kube_deployment_spec_replicas" in query:
            return [
                self._instant_row({"namespace": "ns", "deployment": "api"}, 3),
            ]
        if (
            "kube_deployment_status_replicas" in query
            or "kube_statefulset_replicas" in query
            or "kube_statefulset_status_replicas" in query
            or "kube_daemonset_status_desired_number_scheduled" in query
        ):
            return []
        if "requests_cpu_cores" in query or 'resource="cpu"' in query and "requests" in query:
            rows = [
                self._resource_row("ns", "api-rs-a", "app", 1.0),
                self._resource_row("ns", "api-rs-b", "sidecar", 1.0),
                self._resource_row("ns", "orphan", "app", 10.0),
            ]
            if self._includes_pod_container(query):
                rows.append(self._resource_row("ns", "api-rs-a", "POD", 0.5))
            return rows
        if "limits_cpu_cores" in query or 'resource="cpu"' in query and "limits" in query:
            return [self._resource_row("ns", "api-rs-a", "POD", 0.5)] if self._includes_pod_container(query) else []
        if "requests_memory_bytes" in query or 'resource="memory"' in query and "requests" in query:
            return [self._resource_row("ns", "api-rs-a", "POD", 0.5 * GIB)] if self._includes_pod_container(query) else []
        if "limits_memory_bytes" in query or 'resource="memory"' in query and "limits" in query:
            rows = [
                self._resource_row("ns", "api-rs-a", "app", 1.0 * GIB),
                self._resource_row("ns", "api-rs-b", "sidecar", 1.0 * GIB),
                self._resource_row("ns", "orphan", "app", 10.0 * GIB),
            ]
            if self._includes_pod_container(query):
                rows.append(self._resource_row("ns", "api-rs-a", "POD", 0.5 * GIB))
            return rows
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

    @staticmethod
    def _includes_pod_container(query: str) -> bool:
        return 'container!="POD"' not in query


class AsymmetricResourcePrometheusClient:
    def __init__(self, *args, **kwargs):
        pass

    def query_range(self, query: str, *, start: float, end: float, step: int):
        if "container_cpu_usage_seconds_total" in query:
            return [
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "alertmanager", "node-1", [0.01, 0.02]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "config-reloader", "node-1", [0.01, 0.02]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "alertmanager", "node-2", [0.01, 0.02]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "config-reloader", "node-2", [0.01, 0.02]),
            ]
        if "container_memory_working_set_bytes" in query:
            return [
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "alertmanager", "node-1", [10 * 1024 ** 2, 20 * 1024 ** 2]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-0", "config-reloader", "node-1", [1 * 1024 ** 2, 2 * 1024 ** 2]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "alertmanager", "node-2", [10 * 1024 ** 2, 20 * 1024 ** 2]),
                FakePrometheusClient._range_row("monitoring", "alertmanager-main-1", "config-reloader", "node-2", [1 * 1024 ** 2, 2 * 1024 ** 2]),
            ]
        return []

    def query(self, query: str, *, ts=None):
        if "kube_pod_owner" in query:
            return [
                FakePrometheusClient._instant_row(
                    {
                        "namespace": "monitoring",
                        "pod": "alertmanager-main-0",
                        "owner_kind": "StatefulSet",
                        "owner_name": "alertmanager-main",
                    },
                    1,
                ),
                FakePrometheusClient._instant_row(
                    {
                        "namespace": "monitoring",
                        "pod": "alertmanager-main-1",
                        "owner_kind": "StatefulSet",
                        "owner_name": "alertmanager-main",
                    },
                    1,
                ),
            ]
        if "kube_statefulset_replicas" in query:
            return [FakePrometheusClient._instant_row({"namespace": "monitoring", "statefulset": "alertmanager-main"}, 2)]
        if "requests_cpu_cores" in query or 'resource="cpu"' in query and "requests" in query:
            return []
        if "limits_cpu_cores" in query or 'resource="cpu"' in query and "limits" in query:
            return [
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-0", "config-reloader", 0.1),
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-1", "config-reloader", 0.1),
            ]
        if "requests_memory_bytes" in query or 'resource="memory"' in query and "requests" in query:
            return [
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-0", "alertmanager", 200 * 1024 ** 2),
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-1", "alertmanager", 200 * 1024 ** 2),
            ]
        if "limits_memory_bytes" in query or 'resource="memory"' in query and "limits" in query:
            return [
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-0", "config-reloader", 25 * 1024 ** 2),
                FakePrometheusClient._resource_row("monitoring", "alertmanager-main-1", "config-reloader", 25 * 1024 ** 2),
            ]
        return []


class K8SWorkloadProviderTest(unittest.TestCase):
    def setUp(self):
        FakePrometheusClient.queries = []

    def test_resolve_targets_prefers_file_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k8s_prometheus_clusters.json"
            path.write_text(
                json.dumps([{"cluster": "file-cluster", "prometheus_url": "http://file-prometheus:9090"}]),
                encoding="utf-8",
            )
            with patch.object(provider, "K8S_PROMETHEUS_CONFIG_PATH", path):
                with patch.dict("os.environ", {"K8S_PROMETHEUS_CLUSTERS": '{"env-cluster":"http://env:9090"}'}):
                    targets = provider._resolve_targets()

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].cluster, "file-cluster")
        self.assertEqual(targets[0].prometheus_url, "http://file-prometheus:9090")

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
        self.assertEqual(item["spec"]["replicas"], 3)
        self.assertEqual(item["spec"]["replicas_observed"], 3)
        self.assertEqual(item["spec"]["nodes"], ["node-1", "node-2"])
        self.assertNotIn("cpu_request_cores", item["spec"])
        self.assertNotIn("memory_limit_gb", item["spec"])
        self.assertNotIn("cpu_request_cores_total", item["spec"])
        self.assertNotIn("memory_limit_gb_total", item["spec"])
        self.assertEqual(item["spec"]["cpu_limit_metric_mode"], "cpu_usage_cores")
        self.assertEqual(item["spec"]["cpu_request_metric_mode"], "cpu_usage/cpu_request")
        self.assertEqual(item["spec"]["memory_limit_metric_mode"], "memory_working_set/memory_limit")
        self.assertEqual(item["spec"]["memory_request_metric_mode"], "memory_working_set_gb")
        self.assertEqual(
            item["spec"]["containers"],
            {
                "app": {
                    "cpu_request_cores": 1.0,
                    "cpu_limit_cores": None,
                    "memory_request_gb": None,
                    "memory_limit_gb": 1.0,
                },
                "sidecar": {
                    "cpu_request_cores": 1.0,
                    "cpu_limit_cores": None,
                    "memory_request_gb": None,
                    "memory_limit_gb": 1.0,
                },
            },
        )
        self.assertTrue(any('container!="POD"' in query for query in FakePrometheusClient.queries))
        self.assertEqual(set(item["metrics"]), {"cpu_limit", "cpu_request", "memory_limit", "memory_request"})
        self.assertAlmostEqual(item["metrics"]["cpu_limit"]["values"][0], 0.3)
        self.assertAlmostEqual(item["metrics"]["cpu_limit"]["values"][1], 0.6)
        self.assertAlmostEqual(item["metrics"]["cpu_request"]["values"][0], 0.15)
        self.assertAlmostEqual(item["metrics"]["cpu_request"]["values"][1], 0.3)
        self.assertAlmostEqual(item["metrics"]["memory_limit"]["values"][0], 0.5)
        self.assertAlmostEqual(item["metrics"]["memory_limit"]["values"][1], 0.7)
        self.assertAlmostEqual(item["metrics"]["memory_request"]["values"][0], 1.0)
        self.assertAlmostEqual(item["metrics"]["memory_request"]["values"][1], 1.4)

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
        self.assertEqual(report["counts"]["workload_replica_rows"], 1)
        self.assertEqual(report["counts"]["orphan_container_series"], 1)
        self.assertEqual(report["sample_workloads"], [
            {"namespace": "ns", "workload_kind": "Deployment", "workload_name": "api"}
        ])
        self.assertTrue(any("缺少 owner" in warning for warning in report["warnings"]))
        self.assertEqual(report["errors"], [])

    def test_replicaset_owner_query_does_not_include_pod_selector(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="prod|default",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            provider._diagnose_target(target)

        queries = [q for q in FakePrometheusClient.queries if "kube_replicaset_owner" in q]
        self.assertTrue(queries)
        self.assertTrue(all('pod!=""' not in query for query in queries))
        self.assertTrue(any('namespace=~"prod|default"' in query for query in queries))

    def test_cpu_usage_query_uses_ten_minute_rate_window(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
            rate_window="10m",
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            provider._diagnose_target(target)

        cpu_queries = [q for q in FakePrometheusClient.queries if "container_cpu_usage_seconds_total" in q]
        self.assertTrue(cpu_queries)
        self.assertTrue(all("[10m]" in query for query in cpu_queries))

    def test_fetch_target_uses_configured_cpu_rate_window(self):
        target = PrometheusTarget(
            cluster="cluster-a",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
            rate_window="7m",
        )

        with patch.object(provider, "PrometheusClient", FakePrometheusClient):
            provider._fetch_target(target, limit=0)

        cpu_queries = [q for q in FakePrometheusClient.queries if "container_cpu_usage_seconds_total" in q]
        self.assertTrue(cpu_queries)
        self.assertTrue(all("[7m]" in query for query in cpu_queries))

    def test_fetch_target_keeps_asymmetric_container_requests_and_limits_separate(self):
        target = PrometheusTarget(
            cluster="cluster-k8s-1",
            prometheus_url="http://prometheus.example",
            namespace_regex="",
            bearer_token="",
            basic_auth="",
            history_days=1,
            step_seconds=300,
            request_timeout_seconds=5,
        )

        with patch.object(provider, "PrometheusClient", AsymmetricResourcePrometheusClient):
            items = provider._fetch_target(target, limit=0)

        self.assertEqual(len(items), 1)
        spec = items[0]["spec"]
        self.assertNotIn("cpu_request_cores", spec)
        self.assertNotIn("cpu_limit_cores", spec)
        self.assertNotIn("memory_request_gb", spec)
        self.assertNotIn("memory_limit_gb", spec)
        self.assertEqual(spec["cpu_limit_metric_mode"], "cpu_usage/cpu_limit")
        self.assertEqual(spec["cpu_request_metric_mode"], "cpu_usage_cores")
        self.assertEqual(spec["memory_limit_metric_mode"], "memory_working_set/memory_limit")
        self.assertEqual(spec["memory_request_metric_mode"], "memory_working_set/memory_request")
        self.assertAlmostEqual(spec["containers"]["alertmanager"]["memory_request_gb"], 200 / 1024)
        self.assertIsNone(spec["containers"]["alertmanager"]["cpu_limit_cores"])
        self.assertAlmostEqual(spec["containers"]["config-reloader"]["cpu_limit_cores"], 0.1)
        self.assertAlmostEqual(spec["containers"]["config-reloader"]["memory_limit_gb"], 25 / 1024)

        metrics = items[0]["metrics"]
        self.assertAlmostEqual(metrics["cpu_limit"]["values"][0], 0.1)
        self.assertAlmostEqual(metrics["cpu_limit"]["values"][1], 0.2)
        self.assertAlmostEqual(metrics["cpu_request"]["values"][0], 0.04)
        self.assertAlmostEqual(metrics["cpu_request"]["values"][1], 0.08)
        self.assertAlmostEqual(metrics["memory_limit"]["values"][0], 0.04)
        self.assertAlmostEqual(metrics["memory_limit"]["values"][1], 0.08)
        self.assertAlmostEqual(metrics["memory_request"]["values"][0], 0.05)
        self.assertAlmostEqual(metrics["memory_request"]["values"][1], 0.1)

    def test_data_quality_uses_seconds_for_gap_threshold(self):
        idx = pd.date_range("2026-01-01", periods=24, freq="300s")
        series = pd.Series([1.0] * len(idx), index=idx)

        quality = provider._data_quality(series, step_seconds=300)

        self.assertEqual(quality["level"], "good")
        self.assertEqual(quality["max_gap_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
