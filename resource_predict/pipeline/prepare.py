from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from resource_predict.data.io import coerce_metric_series, write_raw_dataset
from resource_predict.resource_types import metric_names_for_resource

logger = logging.getLogger(__name__)

ExternalProvider = Callable[..., List[Dict[str, Any]]]
MOCK_CPU_SCALE = 3.0
MOCK_CPU_OFFSET = 20.0
MOCK_MEMORY_SCALE = 2.4
MOCK_MEMORY_OFFSET = 15.0
MOCK_DISK_SCALE = 2.2
MOCK_DISK_OFFSET = 10.0


def format_spec_for_log(spec: Any) -> str:
    if isinstance(spec, dict):
        try:
            return json.dumps(spec, ensure_ascii=False, default=str)
        except Exception:
            return str(spec)
    return repr(spec)


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
    """构建 resource_id / spec / cpu|memory|disk(Series) 列表。"""
    if data_provider is not None:
        raw_items = data_provider(resources=resources, n=n, freq=freq)
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("data_provider 返回值必须是非空 list")

        prepared_data: List[Dict[str, Any]] = []
        for idx, item in enumerate(raw_items):
            rid_for_log = f"resource_{idx+1:02d}"
            spec_for_log: Any = {}
            try:
                if not isinstance(item, dict):
                    raise TypeError(
                        f"第 {idx} 项必须为 dict，实际为 {type(item).__name__}"
                    )

                rid = item.get("resource_id") or f"resource_{idx+1:02d}"
                rid_for_log = str(rid)
                spec_raw = item.get("spec", {})
                spec_for_log = spec_raw

                metrics = item.get("metrics", {})
                if not isinstance(metrics, dict):
                    raise ValueError("metrics 字段必须为 dict")

                resource_type = str(item.get("resource_type") or "")
                spec_store = spec_raw if isinstance(spec_raw, dict) else {}
                prepared_item: Dict[str, Any] = {
                    "resource_id": str(rid),
                    "spec": spec_store,
                }
                if resource_type:
                    prepared_item["resource_type"] = resource_type
                if isinstance(item.get("data_quality"), dict):
                    prepared_item["data_quality"] = item["data_quality"]
                for metric_name in metric_names_for_resource(prepared_item):
                    prepared_item[metric_name] = coerce_metric_series(metrics.get(metric_name), metric_name)

                min_len = min(len(prepared_item[m]) for m in metric_names_for_resource(prepared_item))
                if test_size > 0 and min_len <= test_size:
                    raise ValueError(
                        f"有效点数不足：最短序列长度={min_len}，需大于 test_size={test_size}"
                    )

                prepared_data.append(prepared_item)
                if raw_checkpoint_path is not None:
                    write_every = 10
                    if len(prepared_data) % write_every == 0 or idx == len(raw_items) - 1:
                        write_raw_dataset(raw_checkpoint_path, prepared_data, freq=freq)
            except Exception as e:
                msg = (
                    "[data_provider] 跳过异常数据: "
                    f"resource_id={rid_for_log!r}, "
                    f"spec={format_spec_for_log(spec_for_log)}, "
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
        y_cpu = np.clip((y_cpu * MOCK_CPU_SCALE + MOCK_CPU_OFFSET) / 100.0, 0.0, 1.0)
        y_mem = np.clip((y_mem * MOCK_MEMORY_SCALE + MOCK_MEMORY_OFFSET) / 100.0, 0.0, 1.0)
        y_disk = np.clip((y_disk * MOCK_DISK_SCALE + MOCK_DISK_OFFSET) / 100.0, 0.0, 1.0)
        out.append(
            {
                "resource_id": f"resource_{i+1:02d}",
                "spec": {},
                "cpu": y_cpu,
                "memory": y_mem,
                "disk": y_disk,
            }
        )
    return out
