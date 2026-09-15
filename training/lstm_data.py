"""磁盘序列缓存与紧凑窗口索引，不物化全量训练样本。"""
from bisect import bisect_right
from collections import OrderedDict
import hashlib
import json
import os

import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset, Sampler

from resource_predict.data.io import atomic_write_json
from training.data import iter_series


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(args):
    directory = args.out / "data-cache"
    directory.mkdir(exist_ok=True)
    index = directory / "index.json"
    raw_hash = file_hash(args.raw / "raw_index.json")
    if index.exists():
        result = json.loads(index.read_text(encoding="utf-8"))
        if result["source"]["raw_index_sha256"] != raw_hash:
            raise ValueError("raw 数据索引已变化，不能续训同一个 LSTM；请使用原快照或新输出目录")
        for meta in result["series"]:
            path = (directory / meta["file"]).resolve()
            if not path.is_relative_to(directory.resolve()) or file_hash(path) != meta["sha256"]:
                raise ValueError("LSTM 磁盘缓存损坏，不能续训")
        return result
    descriptors, skipped, refs = [], [], {}
    step = None
    for item in iter_series(args.raw, args.resource_type, args.metric, args.level,
                            args.max_resources, args.seed, skipped, refs):
        interval = (item.series.index[1]-item.series.index[0]).total_seconds()
        if step is not None and interval != step:
            raise ValueError("序列采样间隔不同，请分别训练")
        step = interval
        identity = {"resource_id": item.resource_id, "container": item.container, "metric": item.metric}
        filename = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() + ".npy"
        temporary = directory / (filename + ".tmp")
        with temporary.open("wb") as output:
            np.save(output, item.series.to_numpy(dtype="float64"), allow_pickle=False)
        os.replace(temporary, directory / filename)
        descriptors.append({**identity, "file": filename, "sha256": file_hash(directory / filename),
                            "start": item.series.index[0].isoformat(), "points": len(item.series)})
        if len(descriptors) % 100 == 0:
            print(f"已缓存 {len(descriptors)} 条序列", flush=True)
    if not descriptors:
        raise ValueError("没有可用的本地序列")
    sizes = [pd.Timedelta(value).total_seconds()/step for value in (args.lookback, args.horizon)]
    if any(value < 1 or not value.is_integer() for value in sizes):
        raise ValueError("lookback/horizon 必须是采样间隔的正整数倍")
    lookback, horizon = map(int, sizes)
    delta = pd.Timedelta(seconds=step)
    end = max(pd.Timestamp(meta["start"]) + meta["points"]*delta for meta in descriptors)
    valid_end = pd.Timestamp(args.validation_end) if args.validation_end else end-pd.Timedelta(args.test_duration)
    cutoff = pd.Timestamp(args.train_end) if args.train_end else valid_end-pd.Timedelta(args.validation_duration)
    if not cutoff < valid_end < end:
        raise ValueError("必须满足 train_end < validation_end < 数据结束时间")
    kept = []
    for meta in descriptors:
        times = pd.date_range(meta["start"], periods=meta["points"], freq=delta)
        train_count, valid_count = int(times.searchsorted(cutoff)), int(times.searchsorted(valid_end))
        ranges = {"train": [lookback, train_count-horizon+1, args.stride],
                  "validation": [max(train_count, lookback), valid_count-horizon+1, horizon],
                  "test": [max(valid_count, lookback), meta["points"]-horizon+1, horizon]}
        if any(not len(range(*value)) for value in ranges.values()):
            skipped.append({k: meta[k] for k in ("resource_id", "container", "metric")}
                           | {"reason": "insufficient_training_validation_or_test_history"})
            continue
        values = np.load(directory / meta["file"], mmap_mode="r", allow_pickle=False)
        meta.update(mean=float(values[:train_count].mean()), scale=max(float(values[:train_count].std()), 1e-6),
                    ranges=ranges)
        kept.append(meta)
        del values
    if not kept:
        raise ValueError("历史不足，无法构造训练/验证/测试窗口")
    if file_hash(args.raw / "raw_index.json") != raw_hash:
        raise ValueError("缓存准备期间 raw 发生更新，请使用稳定快照并新建输出目录")
    result = {"series": kept, "skipped": skipped, "lookback": lookback, "horizon": horizon,
              "source": {"raw_index_sha256": raw_hash, "resource_refs": refs},
              "boundaries": {"train_end_exclusive": cutoff.isoformat(), "validation_end_exclusive": valid_end.isoformat(),
                             "data_end_exclusive": end.isoformat(), "step_seconds": step}}
    atomic_write_json(index, result, indent=2)
    return result


class Positions:
    """每序列一个 range，通过累计数量定位窗口，内存不随窗口总数增长。"""
    def __init__(self, descriptors, phase):
        self.ranges = [range(*meta["ranges"][phase]) for meta in descriptors]
        self.ends = np.cumsum([len(value) for value in self.ranges]).tolist()

    def __len__(self):
        return self.ends[-1] if self.ends else 0

    def __getitem__(self, index):
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        series = bisect_right(self.ends, index)
        start = self.ends[series-1] if series else 0
        return series, self.ranges[series][index-start]


class DiskValues:
    def __init__(self, directory, descriptors, capacity=4):
        self.directory, self.descriptors = directory, descriptors
        self.capacity, self.cache = capacity, OrderedDict()

    def __getitem__(self, index):
        if index not in self.cache:
            self.cache[index] = np.load(self.directory / self.descriptors[index]["file"], mmap_mode="r", allow_pickle=False)
            if len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
        self.cache.move_to_end(index)
        return self.cache[index]

    def series(self, index, step_seconds):
        meta = self.descriptors[index]
        return pd.Series(self[index], index=pd.date_range(meta["start"], periods=meta["points"],
                                                         freq=pd.Timedelta(seconds=step_seconds)))


class Windows(Dataset):
    def __init__(self, values, positions, lookback, horizon):
        self.values, self.positions = values, positions
        self.lookback, self.horizon = lookback, horizon

    def __len__(self):
        return len(self.positions)

    def __getitem__(self, index):
        series, origin = self.positions[index]
        values, meta = self.values[series], self.values.descriptors[series]
        window = ((values[origin-self.lookback:origin+self.horizon]-meta["mean"])/meta["scale"]).astype(np.float32)
        return torch.from_numpy(window[:self.lookback, None]), torch.from_numpy(window[self.lookback:])


class SeriesSampler(Sampler):
    """打乱序列顺序及各序列内部窗口；不生成全量 randperm。"""
    def __init__(self, positions, seed, epoch, start=0):
        self.positions, self.seed, self.epoch, self.start = positions, seed, epoch, start

    def __len__(self):
        return len(self.positions)-self.start

    def __iter__(self):
        order = np.random.default_rng(self.seed+self.epoch).permutation(len(self.positions.ranges))
        skip = self.start
        for index in order:
            count = len(self.positions.ranges[index])
            if skip >= count:
                skip -= count
                continue
            offsets = np.random.default_rng(np.random.SeedSequence([self.seed, self.epoch, int(index)])).permutation(count)
            base = self.positions.ends[index-1] if index else 0
            for offset in offsets[skip:]:
                yield base+int(offset)
            skip = 0
