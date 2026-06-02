from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from resource_predict.utils import parse_positive_int


VM_SCALING_CONFIG_PATH = Path("deploy") / "clusters.json"
K8S_PROMETHEUS_CONFIG_PATH = Path("deploy") / "k8s_prometheus_clusters.json"
DEFAULT_SSH_KEY = "/root/.ssh/id_rsa"
DEFAULT_OPENSTACK_RC = "/root/admin-openstack.sh"


class ClusterConfigValidationError(ValueError):
    pass


def read_vm_scaling_clusters(path: Path | str = VM_SCALING_CONFIG_PATH) -> Dict[str, Dict[str, Any]]:
    data = _read_json(Path(path), default={})
    if not isinstance(data, dict):
        raise ClusterConfigValidationError("VM 调配集群配置必须是对象")
    return {str(name): dict(item) for name, item in data.items() if isinstance(item, dict)}


def write_vm_scaling_clusters(clusters: Any, path: Path | str = VM_SCALING_CONFIG_PATH) -> Dict[str, Dict[str, Any]]:
    normalized = normalize_vm_scaling_clusters(clusters)
    _write_json(Path(path), normalized)
    return normalized


def normalize_vm_scaling_clusters(clusters: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(clusters, dict):
        raise ClusterConfigValidationError("VM 调配集群配置必须是对象")
    normalized: Dict[str, Dict[str, Any]] = {}
    for name, item in clusters.items():
        cluster = str(name or "").strip()
        if not cluster:
            raise ClusterConfigValidationError("VM 调配集群名称不能为空")
        if not isinstance(item, dict):
            raise ClusterConfigValidationError(f"VM 调配集群 {cluster} 必须是对象")
        cfg = dict(item)
        cfg["cloud_type"] = str(cfg.get("cloud_type") or "openstack").strip() or "openstack"
        _require(cfg, ("control_host", "ssh_user"), f"VM 调配集群 {cluster}")
        cfg["ssh_port"] = parse_positive_int(cfg.get("ssh_port"), default=22)
        cfg["ssh_key"] = str(cfg.get("ssh_key") or DEFAULT_SSH_KEY).strip() or DEFAULT_SSH_KEY
        if cfg["cloud_type"].lower() == "openstack":
            cfg["openstack_rc"] = str(cfg.get("openstack_rc") or DEFAULT_OPENSTACK_RC).strip() or DEFAULT_OPENSTACK_RC
        normalized[cluster] = cfg
    return normalized


def read_k8s_prometheus_clusters(path: Path | str = K8S_PROMETHEUS_CONFIG_PATH) -> List[Dict[str, Any]]:
    data = _read_json(Path(path), default=[])
    return normalize_k8s_prometheus_clusters(data)


def write_k8s_prometheus_clusters(clusters: Any, path: Path | str = K8S_PROMETHEUS_CONFIG_PATH) -> List[Dict[str, Any]]:
    normalized = normalize_k8s_prometheus_clusters(clusters)
    _write_json(Path(path), normalized)
    return normalized


def normalize_k8s_prometheus_clusters(clusters: Any) -> List[Dict[str, Any]]:
    if isinstance(clusters, dict):
        items = []
        for cluster, value in clusters.items():
            if isinstance(value, str):
                items.append({"cluster": cluster, "prometheus_url": value})
            elif isinstance(value, dict):
                items.append({"cluster": cluster, **value})
            else:
                raise ClusterConfigValidationError(f"K8S 监控集群 {cluster} 配置必须是对象或 URL")
    elif isinstance(clusters, list):
        items = clusters
    else:
        raise ClusterConfigValidationError("K8S 监控接入配置必须是数组或对象")

    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ClusterConfigValidationError(f"K8S 监控接入第 {idx} 项必须是对象")
        cfg = dict(item)
        cluster = str(cfg.get("cluster") or "").strip()
        if not cluster:
            raise ClusterConfigValidationError(f"K8S 监控接入第 {idx} 项缺少 cluster")
        if cluster in seen:
            raise ClusterConfigValidationError(f"K8S 监控集群 {cluster} 重复")
        seen.add(cluster)
        _require(cfg, ("prometheus_url",), f"K8S 监控集群 {cluster}")
        cfg["cluster"] = cluster
        cfg["namespace_regex"] = str(cfg.get("namespace_regex") or "").strip()
        cfg["bearer_token"] = str(cfg.get("bearer_token") or "").strip()
        cfg["basic_auth"] = str(cfg.get("basic_auth") or "").strip()
        cfg["rate_window"] = str(cfg.get("rate_window") or "").strip()
        normalized.append(cfg)
    return normalized


def read_cluster_config_payload() -> Dict[str, Any]:
    return {
        "vm_scaling_clusters": read_vm_scaling_clusters(),
        "k8s_prometheus_clusters": read_k8s_prometheus_clusters(),
        "paths": {
            "vm_scaling_clusters": str(VM_SCALING_CONFIG_PATH),
            "k8s_prometheus_clusters": str(K8S_PROMETHEUS_CONFIG_PATH),
        },
    }


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClusterConfigValidationError(f"{path} 不是合法 JSON: {exc}") from exc


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require(cfg: Dict[str, Any], keys: tuple[str, ...], label: str) -> None:
    missing = [key for key in keys if not str(cfg.get(key) or "").strip()]
    if missing:
        raise ClusterConfigValidationError(f"{label} 缺少必填字段: {', '.join(missing)}")
