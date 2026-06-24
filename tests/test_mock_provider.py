from collections import Counter

import numpy as np

from resource_predict.core.decision import build_scaling_advice
from resource_predict.core.k8s_workload_decision import build_k8s_workload_advice
from resource_predict.providers.mock import _mock_k8s_workload_provider, mock_provider


def _values(item, container, metric):
    return item["container_metrics"][container][metric]["values"]


def test_mock_k8s_container_metrics_are_normalized_per_request_and_limit():
    rows = _mock_k8s_workload_provider(resources=1, n=24, freq="h")
    item = rows[0]

    sidecar = item["spec"]["containers"]["sidecar"]
    assert sidecar["memory_request_gb"] == 0.125
    assert sidecar["memory_limit_gb"] == 0.25
    assert sidecar["cpu_request_cores"] == 0.1
    assert sidecar["cpu_limit_cores"] == 0.25

    memory_limit = _values(item, "sidecar", "memory_limit")
    memory_request = _values(item, "sidecar", "memory_request")
    cpu_limit = _values(item, "sidecar", "cpu_limit")
    cpu_request = _values(item, "sidecar", "cpu_request")

    assert memory_limit != memory_request
    assert cpu_limit != cpu_request
    assert memory_request[0] > memory_limit[0]
    assert cpu_request[0] > cpu_limit[0]


def test_default_mock_catalog_contains_18_vms_and_27_workloads():
    rows = mock_provider(resources=45, n=48, freq="h")

    counts = Counter(row.get("resource_type", "openstack_vm") for row in rows)
    assert counts == {"openstack_vm": 18, "k8s_workload": 27}
    assert len({row["resource_id"] for row in rows}) == 45


def test_mock_workloads_cover_kinds_container_counts_and_baselines():
    rows = _mock_k8s_workload_provider(resources=27, n=48, freq="h")

    assert {row["spec"]["workload_kind"] for row in rows} == {
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ReplicaSet",
    }
    assert {len(row["spec"]["containers"]) for row in rows} == {1, 2, 3}

    baseline_profiles = set()
    for row in rows:
        specs = row["spec"]["containers"].values()
        has_request = any(spec["cpu_request_cores"] is not None for spec in specs)
        has_limit = any(spec["cpu_limit_cores"] is not None for spec in specs)
        baseline_profiles.add((has_request, has_limit))
    assert baseline_profiles == {(True, True), (True, False), (False, True), (False, False)}
    assert any(row["data_quality"]["cpu_limit"]["level"] == "poor" for row in rows)


def test_mock_catalog_covers_high_low_hold_and_mixed_signals():
    rows = mock_provider(resources=45, n=96, freq="h")
    vms = [row for row in rows if row.get("resource_type") != "k8s_workload"]
    workloads = [row for row in rows if row.get("resource_type") == "k8s_workload"]

    vm_means = {
        metric: [float(np.mean(row["metrics"][metric]["values"])) for row in vms]
        for metric in ("cpu", "memory", "disk")
    }
    assert all(any(value > 0.6 for value in values) for values in vm_means.values())
    assert all(any(value < 0.2 for value in values) for values in vm_means.values())
    assert any(
        np.mean(row["metrics"]["cpu"]["values"]) > 0.6
        and np.mean(row["metrics"]["memory"]["values"]) < 0.2
        for row in vms
    )

    assert any(
        np.mean(row["metrics"]["cpu_limit"]["values"]) > 0.6
        and np.mean(row["metrics"]["memory_request"]["values"]) < 0.2
        for row in workloads
    )
    assert any(
        0.25 < np.mean(row["metrics"]["cpu_request"]["values"]) < 0.6
        for row in workloads
    )


def test_mock_catalog_reaches_all_primary_decision_outcomes():
    rows = mock_provider(resources=45, n=240, freq="h")
    vm_advice = []
    workload_advice = []

    for row in rows:
        future = {
            metric: np.asarray(payload["values"][-24:], dtype=float)
            for metric, payload in row["metrics"].items()
        }
        if row.get("resource_type") == "k8s_workload":
            container_future = {
                name: {
                    metric: np.asarray(payload["values"][-24:], dtype=float)
                    for metric, payload in metrics.items()
                }
                for name, metrics in row["container_metrics"].items()
            }
            workload_advice.append(
                build_k8s_workload_advice(
                    future,
                    resource=row,
                    container_future_values=container_future,
                )
            )
        else:
            vm_advice.append(build_scaling_advice(future, current_spec=row["spec"]))

    assert {advice["action"] for advice in vm_advice} >= {"scale_out", "scale_in", "hold"}
    assert any(advice["has_mixed_signals"] for advice in vm_advice)
    assert {advice["action"] for advice in workload_advice} >= {
        "scale_out_candidate",
        "scale_in_candidate",
        "hold",
        "insufficient_data",
    }
    assert any(advice["has_mixed_signals"] for advice in workload_advice)
