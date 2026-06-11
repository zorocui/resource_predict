from __future__ import annotations

"""
模拟全量 / 增量数据源（resource_predict.providers.mock）。

给 pipeline / generate_forecasts 的 data_provider 接口用的示例：
- 返回值格式与 pipeline.prepare 中 data_provider 约定一致
- cpu/memory/disk 的 values 为使用率小数，范围 [0, 1]
- 默认模拟 5 个 resources
- 序列长度 n 默认按调用传入（示例在 __main__ 里固定为 240）
"""

from typing import List, Dict, Any

import numpy as np
import pandas as pd

from resource_predict.resource_types import metric_names_for_resource


def _positive_float(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if np.isfinite(n) and n > 0 else None


def _normalize_usage(raw_usage: np.ndarray, base: Any) -> np.ndarray:
    base_value = _positive_float(base)
    if base_value is None:
        return raw_usage
    return raw_usage / base_value


def _sum_positive(values: List[Any]) -> float | None:
    nums = [_positive_float(value) for value in values]
    total = sum(value for value in nums if value is not None)
    return total if total > 0 else None


def _aggregate_normalized(raw_by_container: Dict[str, np.ndarray], bases_by_container: Dict[str, Any]) -> np.ndarray:
    scoped = [
        raw_by_container[name]
        for name, base in bases_by_container.items()
        if _positive_float(base) is not None and name in raw_by_container
    ]
    base_total = _sum_positive(list(bases_by_container.values()))
    if scoped and base_total is not None:
        return np.sum(scoped, axis=0) / base_total
    if raw_by_container:
        return np.sum(list(raw_by_container.values()), axis=0)
    return np.array([], dtype=float)


# ---------------------------------------------------------------------------
# 增量数据模拟：基于现有序列末尾趋势，生成后续数据点
# ---------------------------------------------------------------------------

def _infer_timedelta_from_series(series: pd.Series) -> pd.Timedelta:
    """根据序列索引的中位步长推断采样间隔，失败时退回 1 小时。"""
    if not isinstance(series, pd.Series) or series.empty:
        return pd.Timedelta(hours=1)
    idx = series.index
    if len(idx) < 2:
        return pd.Timedelta(hours=1)
    diffs = idx[1:].asi8 - idx[:-1].asi8
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return pd.Timedelta(hours=1)
    step_ns = float(np.median(diffs.astype(np.float64)))
    td = pd.Timedelta(nanoseconds=step_ns)
    if td <= pd.Timedelta(0) or pd.isna(td):
        return pd.Timedelta(hours=1)
    return td


def mock_incremental_provider(
    prepared_resources: List[Dict[str, Any]],
    points_to_add: int = 1,
    *,
    freq: str = "h",
) -> List[Dict[str, Any]]:
    """
    为每个资源生成新的监控数据点（基于近期趋势 + 噪声续写）。

    参数
    ----
    prepared_resources : list of dict
        当前内存中的 prepared 数据，每项含 resource_id / cpu/memory/disk (pd.Series)
    points_to_add : int
        每个资源新增的数据点数
    freq : str
        时间频率（如 "h"=1小时）

    返回
    ----
    List[Dict] : 每项为 {"resource_id": str, "metrics": {"cpu": {...}, ...}}
        与 data_provider 返回结构一致，可直接合并到现有序列。
    """
    rng = np.random.default_rng()
    new_data: List[Dict[str, Any]] = []

    for res in prepared_resources:
        rid = str(res.get("resource_id", ""))
        result: Dict[str, Any] = {"resource_id": rid, "metrics": {}}

        for metric in metric_names_for_resource(res):
            series = res.get(metric)
            if not isinstance(series, pd.Series) or series.empty:
                result["metrics"][metric] = {"timestamps": [], "values": []}
                continue

            last_ts = series.index[-1]
            step = _infer_timedelta_from_series(series)
            # 取最后 10 个点估算近期均值/趋势/波动
            recent_vals = series.values[-10:].astype(float)

            new_ts_list = []
            new_val_list = []
            current_ts = last_ts
            buf = list(recent_vals[-6:]) if len(recent_vals) >= 2 else [float(recent_vals[-1])]

            for _ in range(points_to_add):
                current_ts = current_ts + step
                arr = np.asarray(buf, dtype=float)
                trend = float(np.mean(np.diff(arr))) if len(arr) >= 2 else 0.0
                noise_std = max(float(np.std(arr)) * 0.25, 0.003)
                next_val = float(arr[-1] + trend + rng.normal(0.0, noise_std))
                # 使用率理论上 [0,1]，但允许微微超出（后续由 prediction clip 处理）
                next_val = float(np.clip(next_val, 0.0, 1.05))

                new_ts_list.append(int(current_ts.timestamp() * 1000))
                new_val_list.append(round(next_val, 6))
                buf.append(next_val)
                if len(buf) > 6:
                    buf.pop(0)

            result["metrics"][metric] = {
                "timestamps": new_ts_list,
                "values": new_val_list,
            }

        new_data.append(result)

    return new_data


def _simulate_metric_by_mode(*, n: int, rng: np.random.Generator, mode: str, freq: str = "h") -> np.ndarray:
    t = np.arange(n, dtype=float)
    # 三种场景：
    # - scale_out：高负载且整体上升，便于触发扩容
    # - scale_in ：低负载且整体平稳/下降，便于触发缩容
    # - hold     ：中位负载小幅波动，便于触发保持
    if mode == "scale_out":
        base = rng.uniform(62, 72)
        slope = rng.uniform(0.08, 0.16)  # 上升趋势
        amp = rng.uniform(6, 10)
        noise_sigma = rng.uniform(1.2, 2.2)
    elif mode == "scale_in":
        base = rng.uniform(8, 15)
        slope = rng.uniform(-0.03, 0.01)  # 低位缓降或近乎持平
        amp = rng.uniform(1.0, 2.8)
        noise_sigma = rng.uniform(0.5, 1.0)
    else:  # hold
        base = rng.uniform(32, 48)
        slope = rng.uniform(-0.02, 0.03)
        amp = rng.uniform(3.0, 6.0)
        noise_sigma = rng.uniform(0.8, 1.6)

    # 根据 freq 计算一日内的步数，用于日周期模拟
    try:
        steps_per_day = int(pd.Timedelta(days=1) / pd.Timedelta(freq))
    except Exception:
        steps_per_day = 24  # 兜底
    daily = amp * np.sin(2 * np.pi * t / steps_per_day + rng.uniform(0, 2 * np.pi))
    noise = rng.normal(0, noise_sigma, size=n)
    y = base + slope * t + daily + noise
    # 归一化到 [0, 1]（与主流程指标刻度一致，非 0~100 百分比）
    return np.clip(y / 100.0, 0.0, 1.0)


def _mock_vm_provider(resources: int, n: int, freq: str) -> List[Dict[str, Any]]:
    """
    data_provider 需要返回：
    [
      {
        "resource_id": "vm-001",
        "spec": {
          "ip": "10.0.0.11",
          "cluster": "cluster-a",
          "cpu_cores": 4,
          "memory_gb": 8,
          "disk_gb": 100
        },
        "metrics": {
          "cpu": {"timestamps": [...], "values": [...]},
          "memory": {"timestamps": [...], "values": [...]},
          "disk": {"timestamps": [...], "values": [...]},
        }
      },
      ...
    ]
    """
    # 为了稳定复现：同时考虑 resources 序号与当前调用 n
    base_seed = 1000 + n

    idx = pd.date_range("2025-01-01", periods=n, freq=freq)
    idx_list = idx.tolist()

    out: List[Dict[str, Any]] = []
    # 常见虚拟机规格池：mock 中循环使用，便于覆盖不同容量档位场景。
    specs = [
        {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
        {"cpu_cores": 2, "memory_gb": 8, "disk_gb": 60},
        {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 80},
        {"cpu_cores": 4, "memory_gb": 16, "disk_gb": 100},
        {"cpu_cores": 8, "memory_gb": 16, "disk_gb": 160},
        {"cpu_cores": 8, "memory_gb": 32, "disk_gb": 200},
    ]
    clusters = ["cluster-a", "cluster-b", "cluster-c"]
    for i in range(resources):
        rid = f"vm-{i+1:03d}"
        mode = ["scale_out", "scale_in", "hold"][i % 3]
        rng_cpu = np.random.default_rng(base_seed + i * 3 + 0)
        rng_mem = np.random.default_rng(base_seed + i * 3 + 1)
        rng_disk = np.random.default_rng(base_seed + i * 3 + 2)
        cpu = _simulate_metric_by_mode(n=n, rng=rng_cpu, mode=mode, freq=freq)
        memory = _simulate_metric_by_mode(n=n, rng=rng_mem, mode=mode, freq=freq)
        disk = _simulate_metric_by_mode(n=n, rng=rng_disk, mode=mode, freq=freq)

        spec = specs[i % len(specs)]
        ip_octet_3 = 10 + ((i // 200) % 200)
        ip_octet_4 = 10 + (i % 200)
        out.append(
            {
                "resource_id": rid,
                "spec": {
                    "ip": f"10.0.{ip_octet_3}.{ip_octet_4}",
                    "cluster": clusters[i % len(clusters)],
                    "cpu_cores": int(spec["cpu_cores"]),
                    "memory_gb": int(spec["memory_gb"]),
                    "disk_gb": int(spec["disk_gb"]),
                },
                "metrics": {
                    "cpu": {"timestamps": idx_list, "values": cpu.astype(float).tolist()},
                    "memory": {
                        "timestamps": idx_list,
                        "values": memory.astype(float).tolist(),
                    },
                    "disk": {"timestamps": idx_list, "values": disk.astype(float).tolist()},
                },
            }
        )

    return out


def _mock_k8s_workload_provider(resources: int, n: int, freq: str) -> List[Dict[str, Any]]:
    base_seed = 7000 + n
    idx = pd.date_range("2025-01-01", periods=n, freq=freq)
    idx_list = idx.tolist()
    clusters = ["cluster-k8s-a", "cluster-k8s-b"]
    namespaces = ["payments", "orders", "platform", "monitoring"]
    owners = [
        ("Deployment", "api-server"),
        ("Deployment", "worker"),
        ("StatefulSet", "redis"),
        ("Deployment", "collector"),
    ]
    node_names = ["worker-01", "worker-02", "worker-03"]
    cpu_requests = [0.25, 0.5, 1.0, None, 0.2]
    cpu_limits = [1.0, 2.0, None, 0.8, None]
    memory_requests = [0.5, 1.0, 2.0, None, 0.25]
    memory_limits = [1.0, 2.0, None, 1.5, None]

    out: List[Dict[str, Any]] = []
    for i in range(resources):
        namespace = namespaces[i % len(namespaces)]
        owner_kind, owner_name = owners[i % len(owners)]
        workload_name = f"{owner_name}-{i % 3}"
        pods = [f"{workload_name}-{1000 + i * 3 + j:04d}" for j in range(3)]
        containers = ["app", "sidecar"] if i % 4 == 0 else ["app"]
        mode = ["scale_out", "scale_in", "hold", "scale_out", "hold"][i % 5]
        rng_cpu = np.random.default_rng(base_seed + i * 5 + 0)
        rng_mem = np.random.default_rng(base_seed + i * 5 + 1)
        cpu = _simulate_metric_by_mode(n=n, rng=rng_cpu, mode=mode, freq=freq)
        memory = _simulate_metric_by_mode(n=n, rng=rng_mem, mode=mode, freq=freq)
        cpu_request = cpu_requests[i % len(cpu_requests)]
        cpu_limit = cpu_limits[i % len(cpu_limits)]
        memory_request = memory_requests[i % len(memory_requests)]
        memory_limit = memory_limits[i % len(memory_limits)]
        total_cpu_base = _sum_positive([cpu_limit, cpu_request]) or 1.0
        total_memory_base = _sum_positive([memory_limit, memory_request]) or 1.0
        container_specs = {
            name: {
                "cpu_request_cores": cpu_request,
                "cpu_limit_cores": cpu_limit,
                "memory_request_gb": memory_request,
                "memory_limit_gb": memory_limit,
            }
            for name in containers
        }
        container_metrics: Dict[str, Dict[str, Any]] = {}
        container_data_quality: Dict[str, Dict[str, Any]] = {}
        container_metric_modes: Dict[str, Dict[str, str]] = {}
        raw_cpu_by_container: Dict[str, np.ndarray] = {}
        raw_memory_by_container: Dict[str, np.ndarray] = {}
        for pos, name in enumerate(containers):
            weight = 0.82 if name == "app" else 0.18
            jitter = np.random.default_rng(base_seed + i * 17 + pos).normal(0, 0.015, size=n)
            c_cpu_raw = np.clip(cpu * total_cpu_base * weight + jitter, 0.0, None)
            c_memory_raw = np.clip(memory * total_memory_base * weight + jitter, 0.0, None)
            raw_cpu_by_container[name] = c_cpu_raw
            raw_memory_by_container[name] = c_memory_raw
            container_metrics[name] = {
                "cpu_limit": {"timestamps": idx_list, "values": _normalize_usage(c_cpu_raw, cpu_limit).astype(float).tolist()},
                "cpu_request": {"timestamps": idx_list, "values": _normalize_usage(c_cpu_raw, cpu_request).astype(float).tolist()},
                "memory_limit": {"timestamps": idx_list, "values": _normalize_usage(c_memory_raw, memory_limit).astype(float).tolist()},
                "memory_request": {"timestamps": idx_list, "values": _normalize_usage(c_memory_raw, memory_request).astype(float).tolist()},
            }
            container_data_quality[name] = {
                "cpu_limit": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
                "cpu_request": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
                "memory_limit": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
                "memory_request": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
            }
            container_metric_modes[name] = {
                "cpu_limit": "cpu_usage/cpu_limit" if cpu_limit else "cpu_usage_cores",
                "cpu_request": "cpu_usage/cpu_request" if cpu_request else "cpu_usage_cores",
                "memory_limit": "memory_working_set/memory_limit" if memory_limit else "memory_working_set_gb",
                "memory_request": "memory_working_set/memory_request" if memory_request else "memory_working_set_gb",
            }
        cpu_limit_values = _aggregate_normalized(
            raw_cpu_by_container,
            {name: container_specs[name]["cpu_limit_cores"] for name in containers},
        )
        cpu_request_values = _aggregate_normalized(
            raw_cpu_by_container,
            {name: container_specs[name]["cpu_request_cores"] for name in containers},
        )
        memory_limit_values = _aggregate_normalized(
            raw_memory_by_container,
            {name: container_specs[name]["memory_limit_gb"] for name in containers},
        )
        memory_request_values = _aggregate_normalized(
            raw_memory_by_container,
            {name: container_specs[name]["memory_request_gb"] for name in containers},
        )
        out.append(
            {
                "resource_id": f"k8s:{clusters[i % len(clusters)]}:{namespace}:{owner_kind.lower()}:{workload_name}",
                "resource_type": "k8s_workload",
                "spec": {
                    "cluster": clusters[i % len(clusters)],
                    "namespace": namespace,
                    "owner_kind": owner_kind,
                    "owner_name": workload_name,
                    "workload_kind": owner_kind,
                    "workload_name": workload_name,
                    "pods_observed": pods,
                    "containers_observed": containers,
                    "replicas_observed": len(pods),
                    "node": node_names[i % len(node_names)],
                    "containers": container_specs,
                    "cpu_limit_metric_mode": "cpu_usage/cpu_limit" if cpu_limit else "cpu_usage_cores",
                    "cpu_request_metric_mode": "cpu_usage/cpu_request" if cpu_request else "cpu_usage_cores",
                    "memory_limit_metric_mode": "memory_working_set/memory_limit" if memory_limit else "memory_working_set_gb",
                    "memory_request_metric_mode": "memory_working_set/memory_request" if memory_request else "memory_working_set_gb",
                },
                "metrics": {
                    "cpu_limit": {"timestamps": idx_list, "values": cpu_limit_values.astype(float).tolist()},
                    "cpu_request": {"timestamps": idx_list, "values": cpu_request_values.astype(float).tolist()},
                    "memory_limit": {"timestamps": idx_list, "values": memory_limit_values.astype(float).tolist()},
                    "memory_request": {"timestamps": idx_list, "values": memory_request_values.astype(float).tolist()},
                },
                "data_quality": {
                    "cpu_limit": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
                    "cpu_request": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
                    "memory_limit": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
                    "memory_request": {"level": "good", "missing_ratio": 0.0, "max_gap_points": 1, "valid_points": n},
                },
                "container_metrics": container_metrics,
                "container_data_quality": container_data_quality,
                "container_metric_modes": container_metric_modes,
            }
        )
    return out


def mock_provider(resources: int, n: int, freq: str) -> List[Dict[str, Any]]:
    if resources <= 1:
        return _mock_vm_provider(resources=resources, n=n, freq=freq)
    workload_count = max(1, resources // 3)
    vm_count = max(1, resources - workload_count)
    return [
        *_mock_vm_provider(resources=vm_count, n=n, freq=freq),
        *_mock_k8s_workload_provider(resources=workload_count, n=n, freq=freq),
    ]


if __name__ == "__main__":
    # 示例：生成 5 个 resources，n=240，频率按小时
    items = mock_provider(resources=5, n=240, freq="h")
    print(f"mock_provider produced: {len(items)} resources")
    print("first resource keys:", items[0].keys())
    print("first metric timestamps[0]:", items[0]["metrics"]["cpu"]["timestamps"][0])
    # print(items)
