from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


DEFAULT_CLUSTER_CONFIG = Path("deploy") / "clusters.json"


class ClusterConfigError(ValueError):
    pass


def load_cluster_configs(path: Path | str = DEFAULT_CLUSTER_CONFIG) -> Dict[str, Dict[str, Any]]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ClusterConfigError(f"集群配置读取失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ClusterConfigError("集群配置必须是对象，key 为 cluster 名称")
    out: Dict[str, Dict[str, Any]] = {}
    for name, item in data.items():
        if isinstance(item, dict):
            out[str(name)] = item
    return out


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

