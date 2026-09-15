"""本地观测读取与严格时间切分；不填补缺口、不访问监控服务。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from resource_predict.data.raw_store import RawResourceStore
from resource_predict.resource_types import metric_names_for_resource, resource_type_of


@dataclass
class SeriesData:
    resource_id: str
    container: str
    series: pd.Series
    mean: float = 0.0
    scale: float = 1.0
    metric: str = ""


def iter_series(raw: Path, resource_type: str, metric: str, level: str,
                max_resources: int, seed: int, skipped, fingerprints):
    """逐资源产生序列，调用方决定是否驻留内存。"""
    store = RawResourceStore(raw, max_cache_items=1)
    ids = sorted(store.resource_ids())
    np.random.default_rng(seed).shuffle(ids)
    matched = 0
    for rid in ids:
        if max_resources and matched >= max_resources:
            break
        item = store.get(rid)
        if resource_type_of(item) != resource_type:
            continue
        matched += 1
        fingerprints[rid] = store.raw_ref(rid)
        groups = item.get("container_metrics", {}).items() if level == "container" else [("", item)]
        metric_names = metric_names_for_resource(item) if metric == "all" else (metric,)
        found = False
        for container, metrics in groups:
            for name in metric_names:
                series = metrics.get(name)
                if not isinstance(series, pd.Series):
                    skipped.append({"resource_id": rid, "container": container, "metric": name,
                                    "reason": "metric_missing"})
                    continue
                found = True
                gaps = np.diff(series.index.asi8)
                if (len(series) < 3 or series.index.hasnans or not series.index.is_unique
                        or not np.isfinite(series.to_numpy()).all()
                        or gaps[0] <= 0 or not np.all(gaps == gaps[0])):
                    skipped.append({"resource_id": rid, "container": container, "metric": name,
                                    "reason": "irregular_or_invalid"})
                    continue
                yield SeriesData(rid, container, series, metric=name)
        if not found:
            skipped.append({"resource_id": rid, "reason": "metric_or_container_missing"})


def load_series(raw: Path, resource_type: str, metric: str, level: str,
                max_resources: int, seed: int):
    skipped, fingerprints = [], {}
    selected = list(iter_series(raw, resource_type, metric, level, max_resources, seed, skipped, fingerprints))
    if not selected:
        raise ValueError("所选类型/层级/指标没有可用的连续本地序列")
    steps = {int(np.diff(item.series.index.asi8)[0]) for item in selected}
    if len(steps) != 1:
        raise ValueError("序列采样间隔不同，请分别运行实验；不自动重采样")
    # 对实际读取的序列求指纹，便于识别后续原始数据变化。
    digest = hashlib.sha256()
    for item in selected:
        digest.update(repr((item.resource_id, item.container, item.metric)).encode("utf-8"))
        digest.update(item.series.index.asi8.tobytes())
        digest.update(item.series.to_numpy(dtype="float64").tobytes())
    return selected, skipped, {"sha256": digest.hexdigest(), "resource_refs": fingerprints}


def split_windows(items, lookback: int, horizon: int, stride: int,
                  validation_duration: str, test_duration: str, train_end=None, validation_end=None):
    step = items[0].series.index[1] - items[0].series.index[0]
    end = max(item.series.index[-1] for item in items) + step
    valid_end = pd.Timestamp(validation_end) if validation_end else end - pd.Timedelta(test_duration)
    cutoff = pd.Timestamp(train_end) if train_end else valid_end - pd.Timedelta(validation_duration)
    if not cutoff < valid_end < end:
        raise ValueError("必须满足 train_end < validation_end < 数据结束时间")
    windows = {phase: [] for phase in ("train", "validation", "test")}
    kept, skipped = [], []
    for item in items:
        times = item.series.index
        train_count = int(times.searchsorted(cutoff))
        valid_count = int(times.searchsorted(valid_end))
        if train_count < lookback + horizon:
            skipped.append({"resource_id": item.resource_id, "container": item.container,
                            "metric": item.metric,
                            "reason": "insufficient_training_history"})
            continue
        local = {
            "train": range(lookback, train_count - horizon + 1, stride),
            "validation": range(max(train_count, lookback), valid_count - horizon + 1, horizon),
            "test": range(max(valid_count, lookback), len(times) - horizon + 1, horizon),
        }
        if any(not len(origins) for origins in local.values()):
            skipped.append({"resource_id": item.resource_id, "container": item.container,
                            "metric": item.metric,
                            "reason": "insufficient_validation_or_test_history"})
            continue
        values = item.series.iloc[:train_count].to_numpy(dtype=float)
        item.mean = float(values.mean())
        item.scale = max(float(values.std()), 1e-6)
        index = len(kept)
        kept.append(item)
        for phase, origins in local.items():
            windows[phase].extend((index, origin) for origin in origins)
    if not kept:
        raise ValueError("历史不足，无法构造训练/验证/测试窗口；请缩短窗口或选择更长的既有数据")
    return kept, windows, skipped, {"train_end_exclusive": cutoff.isoformat(),
                                   "validation_end_exclusive": valid_end.isoformat(),
                                   "data_end_exclusive": end.isoformat(),
                                   "step_seconds": step.total_seconds()}
