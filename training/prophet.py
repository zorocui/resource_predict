"""Prophet 批量训练留档与只加载预测：python -m training.prophet --help。"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from resource_predict.core.forecasting import clip_usage_range
from resource_predict.data.io import atomic_write_json
from resource_predict.data.raw_store import RawResourceStore
from resource_predict.resource_types import resource_type_of


def model_key(resource_id, container, metric):
    return json.dumps([resource_id, container, metric], ensure_ascii=False, separators=(",", ":"))


def train(args):
    from training.prophet_training import train_streaming
    return train_streaming(args)


class SavedProphetModels:
    """只加载已拟合的 JSON；小型 LRU 避免一次驻留全部资源模型。"""

    def __init__(self, directory, resource_type=None, level=None, cache_size=4):
        self.directory = Path(directory).resolve()
        self.manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != 1 or self.manifest.get("model_type") != "prophet":
            raise ValueError("不是支持的 Prophet 模型索引")
        if self.manifest.get("status", "complete") != "complete":
            raise ValueError("训练尚未完成，请先使用 --resume 续训后再加载预测")
        for key, expected in (("resource_type", resource_type), ("level", level)):
            if expected is not None and self.manifest.get(key) != expected:
                raise ValueError(f"Prophet 模型 {key} 与当前数据不匹配")
        self.entries = self.manifest["models"]
        if not self.entries:
            raise ValueError("索引中没有成功训练的 Prophet 模型")
        self.cache = OrderedDict()
        self.cache_size = max(1, int(cache_size))

    def entry(self, resource_id, container, metric):
        key = model_key(resource_id, container, metric)
        if key not in self.entries:
            raise ValueError("缺少该资源/容器/指标的预训练 Prophet；请显式训练，不会自动 fit")
        return self.entries[key]

    def predict(self, resource_id, container, metric, index):
        from prophet.serialize import model_from_json

        entry = self.entry(resource_id, container, metric)
        if not len(index) or index[0] <= pd.Timestamp(entry["train_end"]):
            raise ValueError("预测窗口不得与模型训练数据重叠")
        expected_step = int(entry["step_seconds"] * 1e9)
        if len(index) > 1 and not np.all(np.diff(index.asi8) == expected_step):
            raise ValueError("预测采样间隔与训练模型不一致")
        key = model_key(resource_id, container, metric)
        model = self.cache.get(key)
        if model is None:
            path = (self.directory / entry["file"]).resolve()
            if not path.is_relative_to(self.directory):
                raise ValueError("模型路径超出模型目录")
            serialized = path.read_text(encoding="utf-8")
            if hashlib.sha256(serialized.encode("utf-8")).hexdigest() != entry["sha256"]:
                raise ValueError("Prophet 模型文件校验失败")
            model = model_from_json(serialized)
            if pd.Timestamp(model.history["ds"].max()) != pd.Timestamp(entry["train_end"]):
                raise ValueError("模型训练边界与索引不一致")
            self.cache[key] = model
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        values = model.predict(pd.DataFrame({"ds": index}))["yhat"].to_numpy()
        if len(values) != len(index) or not np.isfinite(values).all():
            raise ValueError("Prophet 预测长度错误或包含非有限值")
        return clip_usage_range(pd.Series(values, index=index), upper=float(entry["upper_bound"]))


def predict(args):
    if args.out.exists():
        raise ValueError("输出目录已存在，请指定新的 --out")
    bank = SavedProphetModels(args.models)
    store = RawResourceStore(args.raw, max_cache_items=1) if args.raw else None
    failures, timing, predicted = [], [], 0
    args.out.mkdir(parents=True)
    with (args.out / "predictions.jsonl").open("w", encoding="utf-8") as output:
        for entry in bank.entries.values():
            identity = {key: entry[key] for key in ("resource_id", "container", "metric")}
            try:
                after = pd.Timestamp(args.after or entry["train_end"])
                if store:
                    item = store.get(entry["resource_id"])
                    if item is None or resource_type_of(item) != bank.manifest["resource_type"]:
                        raise ValueError("raw 中资源不存在或类型不匹配")
                    metrics = item.get("container_metrics", {}).get(entry["container"], {}) if entry["container"] else item
                    series = metrics.get(entry["metric"])
                    if not isinstance(series, pd.Series) or series.empty:
                        raise ValueError("raw 中缺少对应指标")
                    if len(series) > 1 and float(np.median(np.diff(series.index.asi8))) != entry["step_seconds"] * 1e9:
                        raise ValueError("最新 raw 采样间隔与模型不一致，请显式重训")
                    after = series.index[-1]
                train_end = pd.Timestamp(entry["train_end"])
                if after < train_end:
                    raise ValueError("预测起点早于模型训练末尾")
                age = (after-train_end).total_seconds()
                if args.max_age and age > pd.Timedelta(args.max_age).total_seconds():
                    raise ValueError("模型相对观测起点已过期，请显式重训")
                step = float(entry["step_seconds"])
                steps = pd.Timedelta(args.horizon).total_seconds() / step
                if steps < 1 or not steps.is_integer():
                    raise ValueError("horizon 必须是训练采样间隔的正整数倍")
                index = pd.date_range(after + pd.Timedelta(seconds=step), periods=int(steps), freq=pd.Timedelta(seconds=step))
                tick = time.perf_counter()
                result = bank.predict(**identity, index=index)
                seconds = time.perf_counter()-tick
                row = {**identity, "model": "prophet_saved", "model_train_end": entry["train_end"],
                       "forecast_after": after.isoformat(), "model_age_seconds": age,
                       "timestamps": (index.asi8 // 1_000_000).tolist(), "values": result.tolist()}
                output.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                predicted += 1
                timing.append(seconds)
            except Exception as exc:
                failures.append({**identity, "error": str(exc)})
    report = {"predicted": predicted, "failed": len(failures), "failures": failures,
              "fit_calls": 0, "models": str(args.models), "horizon": args.horizon,
              "load_and_predict_seconds": sum(timing), "max_age": args.max_age,
              "note": "只加载预测；新观测仅推进时间起点，不更新模型参数；无真实未来标签，不生成伪误差"}
    atomic_write_json(args.out / "report.json", report, indent=2)
    return report


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    actions = command.add_subparsers(dest="action", required=True)
    fitting = actions.add_parser("train", help="读取既有 raw，批量拟合并保存新模型版本")
    fitting.add_argument("--raw", type=Path, required=True)
    fitting.add_argument("--out", type=Path, required=True)
    fitting.add_argument("--resource-type", choices=["openstack_vm", "k8s_workload"], required=True)
    fitting.add_argument("--level", choices=["resource", "container"], default="resource")
    fitting.add_argument("--metric", default="all")
    fitting.add_argument("--max-resources", type=int, default=100)
    fitting.add_argument("--seed", type=int, default=42)
    fitting.add_argument("--resume", action="store_true", help="在同一模型目录续训，校验并跳过有效检查点")
    fitting.add_argument("--train-end", help="训练截止时间（不含）；省略则使用全部已有观测")
    inference = actions.add_parser("predict", help="只加载已保存模型，不训练、不自动回退拟合")
    inference.add_argument("--models", type=Path, required=True)
    inference.add_argument("--out", type=Path, required=True)
    origin = inference.add_mutually_exclusive_group()
    origin.add_argument("--raw", type=Path, help="用既有 raw 的各指标末尾作为新预测起点")
    origin.add_argument("--after", help="显式指定预测起点；与 raw 均省略时紧接训练末尾")
    inference.add_argument("--horizon", default="24h")
    inference.add_argument("--max-age", help="观测起点与模型训练末尾允许的最大时长，如 24h；过期报错，不重训")
    return command


def main():
    command = parser()
    args = command.parse_args()
    if args.action == "train" and args.max_resources < 0:
        command.error("max-resources 不得小于 0")
    if args.action == "predict" and any(pd.Timedelta(value) <= pd.Timedelta(0)
                                       for value in (args.horizon, args.max_age) if value):
        command.error("horizon/max-age 必须为正时长")
    report = train(args) if args.action == "train" else predict(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["failed"] or (args.action == "train" and not report["available"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
