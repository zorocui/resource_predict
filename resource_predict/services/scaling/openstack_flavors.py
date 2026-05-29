from __future__ import annotations

import json
import logging
import shlex
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from resource_predict.services.scaling.command_runner import run_ssh_command
from resource_predict.utils import parse_float_or_none, require_float


logger = logging.getLogger(__name__)
_CACHE: Dict[str, Dict[str, Any]] = {}
# 保护 _CACHE 的读写，避免多个调配线程并发触发 flavor 发现时出现竞态
_CACHE_LOCK = threading.Lock()


class FlavorDiscoveryError(RuntimeError):
    pass


class NoSuitableFlavorError(FlavorDiscoveryError):
    pass


@dataclass(frozen=True)
class Flavor:
    name: str
    vcpus: float
    memory_gb: float
    disk_gb: float
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cpu_cores": self.vcpus,
            "memory_gb": self.memory_gb,
            "disk_gb": self.disk_gb,
            "raw": self.raw,
        }


def discover_openstack_flavors(cluster_name: str, cluster_config: Dict[str, Any]) -> List[Flavor]:
    cache_seconds = int(cluster_config.get("flavor_cache_seconds", 300))
    cache_key = _cache_key(cluster_name, cluster_config)
    now = time.time()

    # 快速读缓存（锁内），命中直接返回，避免无谓的 SSH 调用
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - float(cached.get("ts", 0)) <= cache_seconds:
            return list(cached.get("flavors", []))

    command = f"{_openstack_prefix(cluster_config)}openstack flavor list -f json"
    timeout = int(cluster_config.get("flavor_discovery_timeout_seconds") or cluster_config.get("command_timeout_seconds", 300))
    logger.info("[scaling] discovering openstack flavors: cluster=%s command=%s", cluster_name, command)
    result = run_ssh_command(cluster_config, command, timeout_seconds=timeout)
    if int(result.get("exit_code", 1)) != 0:
        raise FlavorDiscoveryError(
            f"failed to discover OpenStack flavors for cluster {cluster_name}: {result.get('stderr') or result.get('stdout')}"
        )
    try:
        rows = json.loads(str(result.get("stdout") or "[]"))
    except Exception as exc:
        raise FlavorDiscoveryError(f"OpenStack flavor list output is not valid JSON: {exc}") from exc
    flavors = _parse_flavors(rows)
    allowed = _allowed_flavors(cluster_config)
    if allowed:
        flavors = [flavor for flavor in flavors if flavor.name in allowed]
    if not flavors:
        raise FlavorDiscoveryError(f"no OpenStack flavors discovered for cluster {cluster_name}")

    # 写回缓存（锁内）；其他并发线程若已写入同 key，覆盖无害（数据相同）
    with _CACHE_LOCK:
        _CACHE[cache_key] = {"ts": time.time(), "flavors": flavors}

    logger.info("[scaling] discovered openstack flavors: cluster=%s count=%d", cluster_name, len(flavors))
    return flavors


def select_flavor_for_target(
    *,
    action: str,
    target_spec: Dict[str, Any],
    current_spec: Dict[str, Any],
    flavors: List[Flavor],
) -> tuple[Flavor, List[str]]:
    target_cpu = require_float(target_spec.get("cpu_cores"), "target cpu_cores", error_cls=FlavorDiscoveryError)
    target_mem = require_float(target_spec.get("memory_gb"), "target memory_gb", error_cls=FlavorDiscoveryError)
    target_disk = require_float(target_spec.get("disk_gb"), "target disk_gb", error_cls=FlavorDiscoveryError)
    current_disk = parse_float_or_none(current_spec.get("disk_gb")) or target_disk
    warnings: List[str] = []

    exact = [
        f for f in flavors
        if f.vcpus == target_cpu and f.memory_gb == target_mem and f.disk_gb == target_disk
    ]
    if exact:
        return _smallest(exact), warnings

    action = str(action or "").lower()
    if action == "scale_in":
        candidates = [
            f for f in flavors
            if f.vcpus <= target_cpu and f.memory_gb <= target_mem and f.disk_gb >= current_disk
        ]
        if not candidates:
            raise NoSuitableFlavorError(
                "no suitable OpenStack flavor found for scale_in: "
                f"target={target_cpu}C/{target_mem}G/{target_disk}G current_disk={current_disk}G"
            )
        selected = _largest(candidates)
        warnings.append(_selection_warning("nearest_down", selected, target_cpu, target_mem, target_disk))
        return selected, warnings

    candidates = [
        f for f in flavors
        if f.vcpus >= target_cpu and f.memory_gb >= target_mem and f.disk_gb >= target_disk
    ]
    if not candidates:
        raise NoSuitableFlavorError(
            "no suitable OpenStack flavor found for scale_out: "
            f"target={target_cpu}C/{target_mem}G/{target_disk}G"
        )
    selected = _smallest(candidates)
    warnings.append(_selection_warning("nearest_up", selected, target_cpu, target_mem, target_disk))
    return selected, warnings


def _parse_flavors(rows: Any) -> List[Flavor]:
    if not isinstance(rows, list):
        raise FlavorDiscoveryError("OpenStack flavor list JSON must be a list")
    flavors: List[Flavor] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or row.get("name") or "").strip()
        vcpus = parse_float_or_none(row.get("VCPUs") or row.get("vcpus"))
        ram_mb = parse_float_or_none(row.get("RAM") or row.get("ram"))
        disk_gb = parse_float_or_none(row.get("Disk") or row.get("disk"))
        if not name or vcpus is None or ram_mb is None or disk_gb is None:
            continue
        flavors.append(Flavor(name=name, vcpus=vcpus, memory_gb=ram_mb / 1024.0, disk_gb=disk_gb, raw=row))
    return flavors


def _openstack_prefix(cluster_config: Dict[str, Any]) -> str:
    rc = str(cluster_config.get("openstack_rc", "")).strip()
    if not rc:
        return ""
    return f". {shlex.quote(rc)} && "


def _allowed_flavors(cluster_config: Dict[str, Any]) -> set[str]:
    raw = cluster_config.get("allowed_flavors")
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def _smallest(flavors: List[Flavor]) -> Flavor:
    return sorted(flavors, key=lambda f: (f.vcpus, f.memory_gb, f.disk_gb, f.name))[0]


def _largest(flavors: List[Flavor]) -> Flavor:
    return sorted(flavors, key=lambda f: (f.vcpus, f.memory_gb, f.disk_gb, f.name), reverse=True)[0]


def _selection_warning(policy: str, flavor: Flavor, cpu: float, mem: float, disk: float) -> str:
    return (
        f"target flavor has no exact match; selected {flavor.name} by {policy} "
        f"(actual={_fmt_num(flavor.vcpus)}C/{_fmt_num(flavor.memory_gb)}G/{_fmt_num(flavor.disk_gb)}G, "
        f"target={_fmt_num(cpu)}C/{_fmt_num(mem)}G/{_fmt_num(disk)}G)"
    )


def _cache_key(cluster_name: str, cluster_config: Dict[str, Any]) -> str:
    host = str(cluster_config.get("control_host", "")).strip()
    user = str(cluster_config.get("ssh_user", "")).strip()
    rc = str(cluster_config.get("openstack_rc", "")).strip()
    allowed = ",".join(sorted(_allowed_flavors(cluster_config)))
    return f"{cluster_name}|{user}@{host}|{rc}|{allowed}"


def _fmt_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"
