from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from resource_predict.data.io import coerce_metric_series
from resource_predict.pipeline.windowing import recent_contiguous_segment
from resource_predict.resource_types import metric_names_for_resource

logger = logging.getLogger(__name__)

ExternalProvider = Callable[..., List[Dict[str, Any]]]
MOCK_CPU_SCALE = 3.0
MOCK_CPU_OFFSET = 20.0
MOCK_MEMORY_SCALE = 2.4
MOCK_MEMORY_OFFSET = 15.0
MOCK_DISK_SCALE = 2.2
MOCK_DISK_OFFSET = 10.0

# 中等负载参数：生成 30%-60% 使用率区间的曲线
MEDIUM_CPU_SCALE = 1.2
MEDIUM_CPU_OFFSET = 35.0
MEDIUM_MEMORY_SCALE = 1.0
MEDIUM_MEMORY_OFFSET = 40.0
MEDIUM_DISK_SCALE = 0.8
MEDIUM_DISK_OFFSET = 38.0


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
                container_metrics = _prepare_container_metrics(
                    item.get("container_metrics"),
                    metric_names_for_resource(prepared_item),
                )
                if container_metrics:
                    prepared_item["container_metrics"] = container_metrics
                if isinstance(item.get("container_data_quality"), dict):
                    prepared_item["container_data_quality"] = item["container_data_quality"]
                if isinstance(item.get("container_metric_modes"), dict):
                    prepared_item["container_metric_modes"] = item["container_metric_modes"]

                min_len = min(len(prepared_item[m]) for m in metric_names_for_resource(prepared_item))
                if test_size > 0 and min_len <= test_size:
                    raise ValueError(
                        f"有效点数不足：最短序列长度={min_len}，需大于 test_size={test_size}"
                    )

                prepared_data.append(prepared_item)
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
        # 每 3 个 VM 中有 1 个使用中等负载参数（覆盖 30%-60% 使用率区间）
        is_medium_load = (i % 3 == 2)
        cpu_scale = MEDIUM_CPU_SCALE if is_medium_load else MOCK_CPU_SCALE
        cpu_offset = MEDIUM_CPU_OFFSET if is_medium_load else MOCK_CPU_OFFSET
        mem_scale = MEDIUM_MEMORY_SCALE if is_medium_load else MOCK_MEMORY_SCALE
        mem_offset = MEDIUM_MEMORY_OFFSET if is_medium_load else MOCK_MEMORY_OFFSET
        disk_scale = MEDIUM_DISK_SCALE if is_medium_load else MOCK_DISK_SCALE
        disk_offset = MEDIUM_DISK_OFFSET if is_medium_load else MOCK_DISK_OFFSET

        y_cpu = simulate_curve(n=n, seed=base_seed + i * 3 + 0, freq=freq)
        y_mem = simulate_curve(n=n, seed=base_seed + i * 3 + 1, freq=freq)
        y_disk = simulate_curve(n=n, seed=base_seed + i * 3 + 2, freq=freq)
        y_cpu = np.clip((y_cpu * cpu_scale + cpu_offset) / 100.0, 0.0, 1.0)
        y_mem = np.clip((y_mem * mem_scale + mem_offset) / 100.0, 0.0, 1.0)
        y_disk = np.clip((y_disk * disk_scale + disk_offset) / 100.0, 0.0, 1.0)
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


def _prepare_container_metrics(value: Any, metric_names: tuple[str, ...]) -> Dict[str, Dict[str, pd.Series]]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Dict[str, pd.Series]] = {}
    for container, metrics in value.items():
        name = str(container or "").strip()
        if not name or not isinstance(metrics, dict):
            continue
        metric_out: Dict[str, pd.Series] = {}
        for metric in metric_names:
            if metric not in metrics:
                continue
            metric_out[metric] = coerce_metric_series(metrics.get(metric), f"{name}.{metric}")
        if metric_out:
            out[name] = metric_out
    return out


def prepare_recent_contiguous_forecast_data(
    items: List[Dict[str, Any]],
    *,
    sample_interval_seconds: float,
    max_gap_steps: int,
    test_size: int,
) -> tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Create a Workload forecast copy trimmed independently per metric."""
    prepared: List[Dict[str, Any]] = []
    skips: List[Dict[str, str]] = []
    for source in items:
        item = dict(source)
        rid = str(item.get("resource_id") or "")
        metric_names = metric_names_for_resource(item)
        quality_by_metric = _copy_quality_map(item.get("data_quality"))
        for metric in metric_names:
            series = item.get(metric)
            if not isinstance(series, pd.Series):
                continue
            segment = recent_contiguous_segment(
                series,
                sample_interval_seconds,
                max_gap_steps,
            )
            item[metric] = segment
            quality = quality_by_metric.setdefault(metric, {})
            _update_recent_segment_quality(
                quality,
                original=series,
                segment=segment,
                test_size=test_size,
            )
            if len(segment) <= test_size:
                skips.append(_skip_entry(rid, metric))
        item["data_quality"] = quality_by_metric

        raw_containers = item.get("container_metrics")
        if isinstance(raw_containers, dict):
            containers_out: Dict[str, Dict[str, pd.Series]] = {}
            container_quality = _copy_container_quality(item.get("container_data_quality"))
            for container, metrics in raw_containers.items():
                if not isinstance(metrics, dict):
                    continue
                container_name = str(container)
                metric_out = dict(metrics)
                quality_out = container_quality.setdefault(container_name, {})
                for metric, series in metrics.items():
                    if not isinstance(series, pd.Series):
                        continue
                    segment = recent_contiguous_segment(
                        series,
                        sample_interval_seconds,
                        max_gap_steps,
                    )
                    metric_out[str(metric)] = segment
                    quality = quality_out.setdefault(str(metric), {})
                    _update_recent_segment_quality(
                        quality,
                        original=series,
                        segment=segment,
                        test_size=test_size,
                    )
                    if len(segment) <= test_size:
                        skips.append(
                            _skip_entry(rid, f"container/{container_name}/{metric}")
                        )
                containers_out[container_name] = metric_out
            item["container_metrics"] = containers_out
            item["container_data_quality"] = container_quality
        prepared.append(item)
    return prepared, skips


def _copy_quality_map(value: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(metric): dict(quality) if isinstance(quality, dict) else {}
        for metric, quality in value.items()
    }


def _copy_container_quality(value: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(container): _copy_quality_map(metrics)
        for container, metrics in value.items()
        if isinstance(metrics, dict)
    }


def _update_recent_segment_quality(
    quality: Dict[str, Any],
    *,
    original: pd.Series,
    segment: pd.Series,
    test_size: int,
) -> None:
    quality.update({
        "recent_contiguous_points": int(len(segment)),
        "recent_contiguous_span_hours": round(
            max(0.0, (segment.index[-1] - segment.index[0]).total_seconds()) / 3600.0,
            2,
        ) if len(segment) >= 2 else 0.0,
        "data_end_ms": int(original.index.max().value // 1_000_000),
        "prediction_skipped": len(segment) <= test_size,
    })


def _skip_entry(resource_id: str, metric: str) -> Dict[str, str]:
    return {
        "resource_id": resource_id,
        "metric": metric,
        "reason": "recent_contiguous_segment_too_short",
    }
