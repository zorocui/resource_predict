"""资源类型归一化与指标集定义。

项目仅使用两种资源类型：
- openstack_vm: VM 资源，预测 cpu / memory / disk
- k8s_workload: K8S Workload 资源，预测 cpu_limit / cpu_request / memory_limit / memory_request

所有遗留别名（pod、k8s_pod、k8s、kubernetes、container 等）统一归一到 k8s_workload。
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
    # 所有 K8S 相关字符串统一归一到 k8s_workload
    if raw in {
        "k8s_workload", "k8s_controller", "workload", "controller",
        "k8s_pod", "pod",
        "k8s", "kubernetes", "k8s_container", "container",
    }:
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
