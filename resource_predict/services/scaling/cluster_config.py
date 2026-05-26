from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from resource_predict.services.cluster_configs import (
    ClusterConfigValidationError,
    VM_SCALING_CONFIG_PATH,
    read_vm_scaling_clusters,
)


DEFAULT_CLUSTER_CONFIG = VM_SCALING_CONFIG_PATH


class ClusterConfigError(ValueError):
    pass


def load_cluster_configs(path: Path | str = DEFAULT_CLUSTER_CONFIG) -> Dict[str, Dict[str, Any]]:
    try:
        return read_vm_scaling_clusters(path)
    except ClusterConfigValidationError as exc:
        raise ClusterConfigError(str(exc)) from exc


def get_cluster_config(cluster: str, path: Path | str = DEFAULT_CLUSTER_CONFIG) -> Dict[str, Any]:
    cluster = str(cluster or "").strip()
    if not cluster:
        raise ClusterConfigError("资源缺少 spec.cluster，无法定位控制节点")

    configs = load_cluster_configs(path)
    cfg = configs.get(cluster)
    if not isinstance(cfg, dict):
        raise ClusterConfigError(f"未找到集群 {cluster} 的控制节点配置")
    if not str(cfg.get("control_host", "")).strip():
        raise ClusterConfigError(f"集群 {cluster} 缺少 control_host")
    if not str(cfg.get("ssh_user", "")).strip():
        raise ClusterConfigError(f"集群 {cluster} 缺少 ssh_user")
    return cfg
