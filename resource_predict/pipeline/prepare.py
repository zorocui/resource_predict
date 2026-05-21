from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from resource_predict.data.io import coerce_metric_series, write_raw_dataset

logger = logging.getLogger(__name__)

ExternalProvider = Callable[..., List[Dict[str, Any]]]


def format_vm_spec_for_log(vm_spec: Any) -> str:
    if isinstance(vm_spec, dict):
        try:
            return json.dumps(vm_spec, ensure_ascii=False, default=str)
        except Exception:
            return str(vm_spec)
    return repr(vm_spec)


def simulate_curve(*, n: int, seed: int, freq: str = "h") -> pd.Series:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = rng.uniform(5, 20)
    trend = rng.uniform(-0.02, 0.08) * t
    daily_amp = rng.uniform(0.8, 3.5)
    try:
        steps_per_day = int(pd.Timedelta(days=1) / pd.Timedelta(freq))
    except Exception:
        steps_per_day = 24
    daily = daily_amp * np.sin(2 * np.pi * t / steps_per_day + rng.uniform(0, 2 * np.pi))
    noise = rng.normal(0, rng.uniform(0.4, 1.2), size=n)
    y = base + trend + daily + noise
    idx = pd.date_range("2025-01-01", periods=n, freq=freq)
    return pd.Series(y, index=idx, name="y")


def build_prepared_data(
    *,
    resources: int,
    n: int,
    test_size: int,
    freq: str,
    base_seed: int,
    data_provider: Optional[ExternalProvider],
    cfg: Any,
    raw_checkpoint_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """构建 resource_id / vm_spec / cpu|memory|disk(Series) 列表。"""
    if data_provider is not None:
        raw_items = data_provider(resources=resources, n=n, freq=freq)
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("data_provider 返回值必须是非空 list")

        prepared_data: List[Dict[str, Any]] = []
        for idx, item in enumerate(raw_items):
            rid_for_log = f"resource_{idx+1:02d}"
            vm_spec_for_log: Any = {}
            try:
                if not isinstance(item, dict):
                    raise TypeError(
                        f"第 {idx} 项必须为 dict，实际为 {type(item).__name__}"
                    )

                rid = item.get("resource_id") or f"resource_{idx+1:02d}"
                rid_for_log = str(rid)
                vm_spec_raw = item.get("vm_spec", {})
                vm_spec_for_log = vm_spec_raw

                metrics = item.get("metrics", {})
                if not isinstance(metrics, dict):
                    raise ValueError("metrics 字段必须为 dict")

                cpu_s = coerce_metric_series(metrics.get("cpu"), "cpu")
                mem_s = coerce_metric_series(metrics.get("memory"), "memory")
                disk_s = coerce_metric_series(metrics.get("disk"), "disk")

                min_len = min(len(cpu_s), len(mem_s), len(disk_s))
                if min_len <= test_size:
                    raise ValueError(
                        f"有效点数不足：最短序列长度={min_len}，需大于 test_size={test_size}"
                    )

                vm_spec_store = vm_spec_raw if isinstance(vm_spec_raw, dict) else {}
                prepared_data.append(
                    {
                        "resource_id": str(rid),
                        "vm_spec": vm_spec_store,
                        "cpu": cpu_s,
                        "memory": mem_s,
                        "disk": disk_s,
                    }
                )
                if raw_checkpoint_path is not None:
                    write_every = 10
                    if len(prepared_data) % write_every == 0 or idx == len(raw_items) - 1:
                        write_raw_dataset(raw_checkpoint_path, prepared_data, freq=freq)
            except Exception as e:
                msg = (
                    "[data_provider] 跳过异常数据: "
                    f"resource_id={rid_for_log!r}, "
                    f"vm_spec={format_vm_spec_for_log(vm_spec_for_log)}, "
                    f"原因: {e}"
                )
                logger.error(msg)

        if not prepared_data:
            raise ValueError(
                "data_provider 返回的 list 在校验后无可用资源（全部条目无效或已跳过）"
            )
        return prepared_data

    out: List[Dict[str, Any]] = []
    for i in range(resources):
        y_cpu = simulate_curve(n=n, seed=base_seed + i * 3 + 0, freq=freq)
        y_mem = simulate_curve(n=n, seed=base_seed + i * 3 + 1, freq=freq)
        y_disk = simulate_curve(n=n, seed=base_seed + i * 3 + 2, freq=freq)
        y_cpu = np.clip((y_cpu * cfg.cpu_scale + cfg.cpu_offset) / 100.0, 0.0, 1.0)
        y_mem = np.clip((y_mem * cfg.memory_scale + cfg.memory_offset) / 100.0, 0.0, 1.0)
        y_disk = np.clip((y_disk * cfg.disk_scale + cfg.disk_offset) / 100.0, 0.0, 1.0)
        out.append(
            {
                "resource_id": f"resource_{i+1:02d}",
                "vm_spec": {},
                "cpu": y_cpu,
                "memory": y_mem,
                "disk": y_disk,
            }
        )
    return out
