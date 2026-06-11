"""资源类型归一化与指标集定义。

项目仅使用两种资源类型：
- openstack_vm: VM 资源，预测 cpu / memory / disk
- k8s_workload: K8S Workload 资源，预测 cpu_limit / cpu_request / memory_limit / memory_request

K8S Workload 使用规范资源类型 k8s_workload；Prometheus / Kubernetes 标签中的 pod
只作为上游字段处理，不作为项目资源类型输入。
"""
from __future__ import annotations

METRIC_NAMES = ("cpu", "memory", "disk")
K8S_METRIC_NAMES = ("cpu_limit", "cpu_request", "memory_limit", "memory_request")


def resource_type_of(item: dict) -> str:
    """将原始类型字符串归一化为规范名。

    Args:
        item: 包含 resource_type 字段的字典

    Returns:
        "openstack_vm" 或 "k8s_workload"
    """
    raw = str(item.get("resource_type") or "").strip().lower().replace("-", "_")
    if raw in {"openstack", "openstack_vm", "vm"}:
        return "openstack_vm"
    if raw in {"k8s_workload", "workload", "controller", "k8s", "kubernetes"}:
        return "k8s_workload"
    return raw or "openstack_vm"


def metric_names_for_resource(item: dict) -> tuple[str, ...]:
    """根据资源类型返回对应的指标集。

    Args:
        item: 包含 resource_type 字段的字典

    Returns:
        K8S_METRIC_NAMES (cpu_limit, cpu_request, memory_limit, memory_request) 或 METRIC_NAMES (cpu, memory, disk)
    """
    if resource_type_of(item) == "k8s_workload":
        return K8S_METRIC_NAMES
    return METRIC_NAMES
