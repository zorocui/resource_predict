from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from resource_predict.services.cluster_configs import (
    K8S_PROMETHEUS_CONFIG_PATH,
    VM_SCALING_CONFIG_PATH,
    normalize_k8s_prometheus_clusters,
    normalize_vm_scaling_clusters,
    read_k8s_prometheus_clusters,
    read_vm_scaling_clusters,
    write_k8s_prometheus_clusters,
    write_vm_scaling_clusters,
)
from resource_predict.services.runtime_config import (
    RUNTIME_CONFIG_PATH,
    SUPPORTED_FORECAST_METHODS,
    RuntimeConfigStore,
    normalize_runtime_config,
    runtime_config_load_warnings,
    runtime_config_store,
    runtime_config_to_dict,
    write_runtime_config,
)


METHOD_LABELS = {
    "arima": "ARIMA", "sarima": "SARIMA", "prophet": "Prophet",
    "seasonal_naive": "Seasonal naive", "rolling_mean": "Rolling mean",
}


def read_system_config_payload(
    *,
    runtime_path: Path | str = RUNTIME_CONFIG_PATH,
    vm_path: Path | str = VM_SCALING_CONFIG_PATH,
    k8s_path: Path | str = K8S_PROMETHEUS_CONFIG_PATH,
    store: RuntimeConfigStore = runtime_config_store,
) -> dict[str, Any]:
    return {
        "runtime": runtime_config_to_dict(store.snapshot()),
        "vm_scaling_clusters": read_vm_scaling_clusters(vm_path),
        "k8s_prometheus_clusters": read_k8s_prometheus_clusters(k8s_path),
        "supported_methods": [
            {"key": key, "label": METHOD_LABELS[key]} for key in SUPPORTED_FORECAST_METHODS
        ],
        "warnings": list(runtime_config_load_warnings),
        "paths": {
            "runtime": str(runtime_path),
            "vm_scaling_clusters": str(vm_path),
            "k8s_prometheus_clusters": str(k8s_path),
        },
    }


def _restore(path: Path, existed: bool, content: bytes) -> None:
    if not existed:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.restore.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def save_system_config_payload(
    payload: Any,
    *,
    runtime_path: Path | str = RUNTIME_CONFIG_PATH,
    vm_path: Path | str = VM_SCALING_CONFIG_PATH,
    k8s_path: Path | str = K8S_PROMETHEUS_CONFIG_PATH,
    store: RuntimeConfigStore = runtime_config_store,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    runtime = normalize_runtime_config(payload.get("runtime", {}))
    vm = normalize_vm_scaling_clusters(payload.get("vm_scaling_clusters", {}))
    k8s = normalize_k8s_prometheus_clusters(payload.get("k8s_prometheus_clusters", []))
    targets = [Path(runtime_path), Path(vm_path), Path(k8s_path)]
    backups = [(path.exists(), path.read_bytes() if path.exists() else b"") for path in targets]
    try:
        write_runtime_config(runtime, targets[0])
        write_vm_scaling_clusters(vm, targets[1])
        write_k8s_prometheus_clusters(k8s, targets[2])
    except Exception:
        for path, (existed, content) in zip(targets, backups):
            _restore(path, existed, content)
        raise
    store.replace(runtime)
    from resource_predict.services.k8s_ingest import notify_k8s_scheduler_config_changed

    notify_k8s_scheduler_config_changed()
    return read_system_config_payload(
        runtime_path=targets[0], vm_path=targets[1], k8s_path=targets[2], store=store,
    )

