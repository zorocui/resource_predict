from __future__ import annotations

from resource_predict.resource_types import metric_names_for_resource, resource_type_of


def test_resource_type_accepts_current_k8s_workload_names():
    for raw in ("k8s_workload", "workload", "controller", "k8s", "kubernetes"):
        assert resource_type_of({"resource_type": raw}) == "k8s_workload"


def test_resource_type_rejects_removed_k8s_legacy_names():
    for raw in ("pod", "k8s_pod", "container", "k8s_container", "k8s_controller"):
        assert resource_type_of({"resource_type": raw}) == raw
        assert metric_names_for_resource({"resource_type": raw}) == ("cpu", "memory", "disk")
