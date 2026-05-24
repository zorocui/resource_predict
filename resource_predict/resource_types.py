from __future__ import annotations

METRIC_NAMES = ("cpu", "memory", "disk")
K8S_METRIC_NAMES = ("cpu", "memory")
POD_METRIC_NAMES = K8S_METRIC_NAMES


def resource_type_of(item: dict) -> str:
    raw = str(item.get("resource_type") or "").strip().lower().replace("-", "_")
    if raw in {"k8s_workload", "k8s_controller", "workload", "controller"}:
        return "k8s_workload"
    if raw in {"k8s_pod", "pod"}:
        return "k8s_pod"
    if raw in {"k8s", "kubernetes", "k8s_container", "container"}:
        return "k8s_container"
    if raw in {"openstack", "openstack_vm", "vm"}:
        return "openstack_vm"
    return raw or "openstack_vm"


def metric_names_for_resource(item: dict) -> tuple[str, ...]:
    if resource_type_of(item) in {"k8s_pod", "k8s_workload"}:
        return K8S_METRIC_NAMES
    return METRIC_NAMES
