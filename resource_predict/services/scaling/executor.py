from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List

from resource_predict.services.scaling.openstack_flavors import NoSuitableFlavorError, discover_openstack_flavors, select_flavor_for_target


class ScalingPlanError(ValueError):
    pass


@dataclass(frozen=True)
class ScalingPlan:
    resource_id: str
    resource_type: str
    cluster: str
    action: str
    commands: List[str]
    warnings: List[str]
    target_spec: Dict[str, Any]
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "cluster": self.cluster,
            "action": self.action,
            "commands": self.commands,
            "warnings": self.warnings,
            "target_spec": self.target_spec,
            "details": self.details,
        }


def build_scaling_plan(
    resource: Dict[str, Any],
    cluster_config: Dict[str, Any],
    *,
    allow_create_flavor: bool = False,
) -> ScalingPlan:
    resource_id = str(resource.get("resource_id", "")).strip()
    if not resource_id:
        raise ScalingPlanError("resource is missing resource_id")
    spec = resource.get("spec", {})
    if not isinstance(spec, dict):
        raise ScalingPlanError(f"resource {resource_id} is missing spec")
    advice = resource.get("scaling_advice", {})
    if not isinstance(advice, dict):
        raise ScalingPlanError(f"resource {resource_id} is missing scaling_advice")
    action = str(advice.get("action", "hold")).lower()
    if action == "hold":
        raise ScalingPlanError("hold advice does not need scaling")
    target = advice.get("target_spec", {})
    if not isinstance(target, dict):
        raise ScalingPlanError("scaling advice is missing target_spec")
    cluster = str(spec.get("cluster") or resource.get("cluster") or "").strip()
    if not cluster:
        raise ScalingPlanError("resource is missing cluster")

    resource_type = _detect_resource_type(resource, spec, cluster_config)
    if resource_type == "openstack_vm":
        commands, warnings, details = _build_openstack_commands(
            resource_id,
            cluster,
            action,
            spec,
            target,
            cluster_config,
            allow_create_flavor=allow_create_flavor,
        )
    elif resource_type in {"k8s_container", "k8s_workload"}:
        commands, warnings, details = _build_k8s_commands(resource_id, spec, target, cluster_config)
    else:
        raise ScalingPlanError(f"unsupported resource type: {resource_type}")
    if not commands:
        raise ScalingPlanError("no executable scaling command was generated")
    return ScalingPlan(resource_id, resource_type, cluster, action, commands, warnings, target, details)


def _detect_resource_type(
    resource: Dict[str, Any],
    spec: Dict[str, Any],
    cluster_config: Dict[str, Any],
) -> str:
    raw = (
        resource.get("resource_type")
        or spec.get("cloud_type")
        or cluster_config.get("cloud_type")
        or cluster_config.get("type")
        or ""
    )
    value = str(raw).strip().lower().replace("-", "_")
    aliases = {
        "openstack": "openstack_vm",
        "openstack_vm": "openstack_vm",
        "vm": "openstack_vm",
        "k8s": "k8s_container",
        "kubernetes": "k8s_container",
        "container": "k8s_container",
        "k8s_container": "k8s_container",
        "k8s_workload": "k8s_workload",
        "workload": "k8s_workload",
    }
    if value in aliases:
        return aliases[value]
    if spec.get("namespace") and (
        spec.get("deployment")
        or spec.get("statefulset")
        or spec.get("workload_name")
        or spec.get("owner_name")
    ):
        return "k8s_workload"
    if spec.get("instance_id") or spec.get("server_id"):
        return "openstack_vm"
    return value


def _build_openstack_commands(
    resource_id: str,
    cluster: str,
    action: str,
    spec: Dict[str, Any],
    target: Dict[str, Any],
    cluster_config: Dict[str, Any],
    *,
    allow_create_flavor: bool = False,
) -> tuple[List[str], List[str], Dict[str, Any]]:
    instance_id = str(spec.get("instance_id") or spec.get("server_id") or "").strip()
    if not instance_id:
        raise ScalingPlanError(f"OpenStack resource {resource_id} is missing instance_id/server_id")

    warnings: List[str] = []
    details: Dict[str, Any] = {"flavor_discovery": "remote", "instance_id": instance_id}
    direct_flavor = str(target.get("target_flavor") or target.get("flavor") or "").strip()
    if direct_flavor:
        target_flavor = direct_flavor
        selected_direct = _find_openstack_flavor_by_name(cluster, cluster_config, target_flavor, warnings)
        if selected_direct is not None:
            details["selected_flavor"] = {**selected_direct.to_dict(), "source": "target_spec"}
        else:
            details["selected_flavor"] = {"name": target_flavor, "source": "target_spec"}
    else:
        flavors = discover_openstack_flavors(cluster, cluster_config)
        try:
            selected, flavor_warnings = select_flavor_for_target(
                action=action,
                target_spec=target,
                current_spec=spec,
                flavors=flavors,
            )
            target_flavor = selected.name
            warnings.extend(flavor_warnings)
            details["selected_flavor"] = selected.to_dict()
        except NoSuitableFlavorError:
            if not allow_create_flavor:
                raise ScalingPlanError(
                    "no suitable OpenStack flavor found; frontend confirmation is required before creating a new flavor"
                )
            new_flavor = _new_flavor_spec(resource_id, spec, target, cluster_config)
            target_flavor = str(new_flavor["name"])
            warnings.append(
                "no suitable OpenStack flavor exists; a new flavor will be created before resizing this resource"
            )
            details["selected_flavor"] = {**new_flavor, "source": "auto_create"}
        details["available_flavor_count"] = len(flavors)

    prefix = _openstack_prefix(cluster_config)
    commands = []
    selected_flavor = details.get("selected_flavor", {})
    if isinstance(selected_flavor, dict) and selected_flavor.get("source") == "auto_create":
        commands.append(
            f"{prefix}openstack flavor create"
            f" --vcpus {int(float(selected_flavor['cpu_cores']))}"
            f" --ram {int(round(float(selected_flavor['memory_gb']) * 1024))}"
            f" --disk {int(float(selected_flavor['disk_gb']))}"
            f" {shlex.quote(target_flavor)}"
        )
    commands.append(f"{prefix}openstack server resize --flavor {shlex.quote(target_flavor)} {shlex.quote(instance_id)}")
    if bool(cluster_config.get("auto_confirm_resize", False)):
        commands.append(build_openstack_resize_confirm_command(instance_id, cluster_config))

    current_disk = _num(spec.get("disk_gb"))
    target_disk = _num(target.get("disk_gb"))
    if current_disk is not None and target_disk is not None and target_disk < current_disk:
        warnings.append("OpenStack disk shrink is not automated in phase 1; handle disk reduction manually")
    elif current_disk is not None and target_disk is not None and target_disk > current_disk:
        warnings.append("OpenStack disk/filesystem expansion is not automated in phase 1; only server flavor resize is generated")
    return commands, warnings, details


def _find_openstack_flavor_by_name(
    cluster: str,
    cluster_config: Dict[str, Any],
    flavor_name: str,
    warnings: List[str],
):
    try:
        flavors = discover_openstack_flavors(cluster, cluster_config)
    except Exception as exc:
        warnings.append(f"could not resolve target flavor spec for local snapshot: {exc}")
        return None
    for flavor in flavors:
        if flavor.name == flavor_name:
            return flavor
    warnings.append(
        f"target flavor {flavor_name} was not found in discovered flavor list; local snapshot will use target_spec fields only"
    )
    return None


def _new_flavor_spec(
    resource_id: str,
    spec: Dict[str, Any],
    target: Dict[str, Any],
    cluster_config: Dict[str, Any],
) -> Dict[str, Any]:
    cpu = _required_num(target.get("cpu_cores"), "target cpu_cores")
    memory = _required_num(target.get("memory_gb"), "target memory_gb")
    target_disk = _required_num(target.get("disk_gb"), "target disk_gb")
    current_disk = _num(spec.get("disk_gb")) or target_disk
    disk = max(target_disk, current_disk)
    prefix = str(cluster_config.get("auto_flavor_name_prefix") or "rp").strip() or "rp"
    name = str(target.get("auto_flavor_name") or "").strip()
    if not name:
        name = f"{prefix}-{_fmt_spec_part(cpu)}c-{_fmt_spec_part(memory)}g-{_fmt_spec_part(disk)}d"
    return {
        "name": name,
        "cpu_cores": cpu,
        "memory_gb": memory,
        "disk_gb": disk,
        "resource_id": resource_id,
    }


def _openstack_prefix(cluster_config: Dict[str, Any]) -> str:
    rc = str(cluster_config.get("openstack_rc", "")).strip()
    if not rc:
        return ""
    return f". {shlex.quote(rc)} && "


def build_openstack_resize_confirm_command(instance_id: str, cluster_config: Dict[str, Any]) -> str:
    prefix = _openstack_prefix(cluster_config)
    return f"{prefix}{_openstack_resize_confirm_command_body(instance_id, cluster_config)}"


def _openstack_resize_confirm_command_body(instance_id: str, cluster_config: Dict[str, Any]) -> str:
    interval = _positive_int(cluster_config.get("resize_confirm_poll_interval_seconds"), 15)
    wait_seconds = _positive_int(cluster_config.get("resize_confirm_wait_seconds"), 240)
    interval = max(5, interval)
    attempts = max(1, (wait_seconds + interval - 1) // interval)
    instance = shlex.quote(instance_id)
    return (
        f"for i in $(seq 1 {attempts}); do "
        f"status=$(openstack server show -f value -c status {instance} 2>/dev/null | tr -d '\\r'); "
        f"echo \"resize status: $status\"; "
        f"if [ \"$status\" = \"VERIFY_RESIZE\" ]; then "
        f"openstack server resize --confirm {instance}; exit $?; "
        f"fi; "
        f"if [ \"$status\" = \"ERROR\" ]; then "
        f"echo \"resize entered ERROR\" >&2; exit 1; "
        f"fi; "
        f"sleep {interval}; "
        f"done; "
        f"echo \"timed out waiting for VERIFY_RESIZE\" >&2; exit 124"
    )


def _build_k8s_commands(
    resource_id: str,
    spec: Dict[str, Any],
    target: Dict[str, Any],
    cluster_config: Dict[str, Any],
) -> tuple[List[str], List[str], Dict[str, Any]]:
    namespace = str(spec.get("namespace") or "default").strip()
    workload_kind = str(spec.get("workload_kind") or spec.get("owner_kind") or "").strip().lower()
    workload_name = str(spec.get("workload_name") or spec.get("owner_name") or "").strip()
    if not workload_name and spec.get("deployment"):
        workload_kind = "deployment"
        workload_name = str(spec.get("deployment")).strip()
    if not workload_name and spec.get("statefulset"):
        workload_kind = "statefulset"
        workload_name = str(spec.get("statefulset")).strip()
    if workload_kind in {"deploy", "deployment"}:
        workload_kind = "deployment"
    elif workload_kind in {"sts", "statefulset"}:
        workload_kind = "statefulset"
    elif workload_kind in {"ds", "daemonset"}:
        workload_kind = "daemonset"
    elif workload_kind in {"rs", "replicaset"}:
        workload_kind = "replicaset"
    if not workload_name:
        raise ScalingPlanError(f"K8S resource {resource_id} is missing workload_name/deployment/statefulset/daemonset")
    if workload_kind not in {"deployment", "statefulset", "daemonset", "replicaset"}:
        raise ScalingPlanError(f"K8S workload kind {workload_kind or '-'} is not supported")
    container = str(spec.get("container") or "").strip()
    observed_containers = spec.get("containers_observed")
    if not container and isinstance(observed_containers, list) and len(observed_containers) == 1:
        container = str(observed_containers[0] or "").strip()

    cpu_request = _num(target.get("cpu_request_cores") or target.get("cpu_cores"))
    cpu_limit = _num(target.get("cpu_limit_cores") or target.get("cpu_cores"))
    memory_request = _num(target.get("memory_request_gb") or target.get("memory_gb"))
    memory_limit = _num(target.get("memory_limit_gb") or target.get("memory_gb"))
    replicas = _positive_int(target.get("replicas"), 0)

    kubeconfig = str(cluster_config.get("kubeconfig", "")).strip()
    kube = "kubectl"
    if kubeconfig:
        kube += f" --kubeconfig {shlex.quote(kubeconfig)}"
    target_ref = f"{workload_kind}/{workload_name}"
    warnings: List[str] = []
    commands: List[str] = []
    container_targets = _container_targets(target)
    if container_targets:
        for target_container, container_target in container_targets:
            cmd = _build_k8s_resource_command(kube, namespace, target_ref, target_container, container_target)
            if cmd:
                commands.append(cmd)
    elif cpu_request is not None or memory_request is not None or cpu_limit is not None or memory_limit is not None:
        container_target = {
            "cpu_request_cores": cpu_request,
            "cpu_limit_cores": cpu_limit,
            "memory_request_gb": memory_request,
            "memory_limit_gb": memory_limit,
        }
        cmd = _build_k8s_resource_command(kube, namespace, target_ref, container, container_target)
        if not container:
            warnings.append("no single container was identified; kubectl will apply resources to all containers")
        if cmd:
            commands.append(cmd)
    if replicas > 0 and workload_kind != "daemonset":
        current_replicas = _positive_int(spec.get("replicas") or spec.get("replicas_observed"), 0)
        if current_replicas != replicas:
            commands.append(f"{kube} -n {shlex.quote(namespace)} scale {shlex.quote(target_ref)} --replicas={replicas}")
    elif replicas > 0 and workload_kind == "daemonset":
        warnings.append("DaemonSet replicas are controlled by node scheduling; replica scaling command was skipped")
    current_disk = _num(spec.get("disk_gb"))
    target_disk = _num(target.get("disk_gb"))
    if current_disk is not None and target_disk is not None and target_disk != current_disk:
        warnings.append("K8S phase 1 only adjusts CPU/memory requests/limits; storage capacity is not changed")
    return commands, warnings, {
        "workload": {
            "kind": workload_kind,
            "name": workload_name,
            "namespace": namespace,
            "container": container,
            "replicas": replicas if replicas > 0 else None,
        }
    }


def _container_targets(target: Dict[str, Any]) -> List[tuple[str, Dict[str, Any]]]:
    raw = target.get("containers")
    if not isinstance(raw, dict):
        return []
    out: List[tuple[str, Dict[str, Any]]] = []
    for name, values in raw.items():
        container = str(name or "").strip()
        if not container or not isinstance(values, dict):
            continue
        normalized = {
            "cpu_request_cores": _num(values.get("cpu_request_cores")),
            "cpu_limit_cores": _num(values.get("cpu_limit_cores")),
            "memory_request_gb": _num(values.get("memory_request_gb")),
            "memory_limit_gb": _num(values.get("memory_limit_gb")),
        }
        if any(value is not None for value in normalized.values()):
            out.append((container, normalized))
    return out


def _build_k8s_resource_command(
    kube: str,
    namespace: str,
    target_ref: str,
    container: str,
    target: Dict[str, Any],
) -> str:
    request_parts = []
    limit_parts = []
    cpu_request = _num(target.get("cpu_request_cores") or target.get("cpu_cores"))
    cpu_limit = _num(target.get("cpu_limit_cores") or target.get("cpu_cores"))
    memory_request = _num(target.get("memory_request_gb") or target.get("memory_gb"))
    memory_limit = _num(target.get("memory_limit_gb") or target.get("memory_gb"))
    if cpu_request is not None:
        request_parts.append(f"cpu={_format_k8s_cpu(cpu_request)}")
    if memory_request is not None:
        request_parts.append(f"memory={_format_k8s_memory(memory_request)}")
    if cpu_limit is not None:
        limit_parts.append(f"cpu={_format_k8s_cpu(cpu_limit)}")
    if memory_limit is not None:
        limit_parts.append(f"memory={_format_k8s_memory(memory_limit)}")
    if not request_parts and not limit_parts:
        return ""
    container_arg = f" --containers={shlex.quote(container)}" if container else ""
    cmd = f"{kube} -n {shlex.quote(namespace)} set resources {shlex.quote(target_ref)}{container_arg}"
    if request_parts:
        cmd += f" --requests={','.join(request_parts)}"
    if limit_parts:
        cmd += f" --limits={','.join(limit_parts)}"
    return cmd


def _format_k8s_cpu(cpu: float) -> str:
    if float(cpu).is_integer():
        return str(int(cpu))
    return f"{int(round(cpu * 1000))}m"


def _format_k8s_memory(memory_gb: float) -> str:
    if float(memory_gb).is_integer():
        return f"{int(memory_gb)}Gi"
    return f"{int(round(memory_gb * 1024))}Mi"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _required_num(value: Any, name: str) -> float:
    n = _num(value)
    if n is None:
        raise ScalingPlanError(f"missing {name}")
    return n


def _positive_int(value: Any, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


def _fmt_spec_part(value: float) -> str:
    text = str(int(value)) if float(value).is_integer() else f"{value:.2f}"
    return text.replace(".", "p")

