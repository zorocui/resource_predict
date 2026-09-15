"""Prophet 流式训练与逐模型原子检查点；只读访问既有 raw。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import logging
import os
import time

import numpy as np
import pandas as pd

from resource_predict.core.forecasting import usage_forecast_upper_bound
from resource_predict.data.io import atomic_write_json, atomic_write_text
from resource_predict.data.raw_store import RawResourceStore
from resource_predict.resource_types import metric_names_for_resource
from resource_predict.settings import settings
from training.prophet import model_key


@contextmanager
def training_lock(directory):
    """操作系统锁在进程退出时释放；锁文件保留，不靠删除锁文件解锁。"""
    with (directory / ".training.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("该模型目录已有训练进程，请勿同时续训") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def iter_histories(args, store, skipped, failures):
    """只驻留当前资源及其容器序列，不构造全量时序列表。"""
    ids = sorted(store.resource_ids())
    np.random.default_rng(args.seed).shuffle(ids)
    matched = 0
    cutoff = pd.Timestamp(args.train_end) if args.train_end else None
    for rid in ids:
        if args.max_resources and matched >= args.max_resources:
            break
        if store.raw_ref(rid).get("resource_type") != args.resource_type:
            continue
        matched += 1
        try:
            item = store.get(rid)
        except Exception as exc:
            failures.append({"resource_id": rid, "error": f"raw 读取失败: {exc}"})
            continue
        names = metric_names_for_resource(item) if args.metric == "all" else (args.metric,)
        groups = item.get("container_metrics", {}).items() if args.level == "container" else [("", item)]
        found = False
        for container, metrics in groups:
            for name in names:
                identity = {"resource_id": rid, "container": container, "metric": name}
                series = metrics.get(name)
                if not isinstance(series, pd.Series):
                    skipped.append({**identity, "reason": "metric_missing"})
                    continue
                found = True
                history = series if cutoff is None else series[series.index < cutoff]
                if len(history) < 48:
                    failures.append({**identity, "error": "训练截止点前不足 48 个有效点"})
                    continue
                gaps = np.diff(history.index.asi8)
                if (history.index.hasnans or not np.isfinite(history.to_numpy()).all()
                        or gaps[0] <= 0 or not np.all(gaps == gaps[0])):
                    failures.append({**identity, "error": "训练历史不连续或含非有限值，不自动插值"})
                    continue
                yield identity, history
        if not found:
            skipped.append({"resource_id": rid, "reason": "metric_or_container_missing"})
        # 生成器不得在下一资源读取期间保留上一资源的额外时序引用。
        del item, groups


def fingerprint(identity, history):
    digest = hashlib.sha256(model_key(**identity).encode("utf-8"))
    digest.update(history.index.asi8.tobytes())
    digest.update(history.to_numpy(dtype="float64").tobytes())
    return digest.hexdigest()


def reusable_entry(directory, checkpoint, data_hash, config_hash):
    """检查点损坏或模型损坏只使本条失效，不伪装为已完成。"""
    try:
        entry = json.loads(checkpoint.read_text(encoding="utf-8"))
        if entry["data_sha256"] != data_hash or entry["config_sha256"] != config_hash:
            return None
        path = (directory / entry["file"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            return None
        return entry
    except (OSError, ValueError, KeyError, TypeError):
        return None


def train_streaming(args):
    from prophet import Prophet, __version__
    from prophet.serialize import model_to_json
    from cmdstanpy.utils import get_logger

    resume = args.resume
    if args.out.exists() and not resume:
        raise ValueError("模型目录已存在；续训请添加 --resume，重训新版本请指定新 --out")
    if resume and not (args.out / "run.json").is_file():
        raise ValueError("没有可续训的 run.json；旧版模型请保留并另建新目录训练")
    # 先确认输入有效，避免在错误路径下建立空训练任务。
    store = RawResourceStore(args.raw, max_cache_items=1)
    store.resource_ids()
    get_logger().setLevel(logging.WARNING)
    forecast = asdict(settings.forecast)
    parameters = {key.removeprefix("prophet_"): forecast[key] for key in (
        "prophet_seasonality_mode", "prophet_daily_seasonality", "prophet_weekly_seasonality",
        "prophet_yearly_seasonality", "prophet_changepoint_prior_scale", "prophet_seasonality_prior_scale")}
    parameters["uncertainty_samples"] = 0
    configuration = {"resource_type": args.resource_type, "level": args.level, "metric": args.metric,
                     "train_end": args.train_end, "seed": args.seed, "parameters": parameters,
                     "prophet_version": __version__, "raw": str(args.raw.resolve()),
                     "clip": {k: v for k, v in forecast.items() if k.startswith("usage_clip_")}}
    config_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode("utf-8")).hexdigest()
    args.out.mkdir(parents=True, exist_ok=resume)
    with training_lock(args.out):
        if resume:
            previous = json.loads((args.out / "run.json").read_text(encoding="utf-8"))
            if previous.get("config_sha256") != config_hash:
                raise ValueError("续训配置、源目录或 Prophet 版本不一致，请另建模型目录")
            if previous["max_resources"] == 0 and args.max_resources != 0:
                raise ValueError("续训不能缩小资源范围")
            if args.max_resources and args.max_resources < previous["max_resources"]:
                raise ValueError("续训不能缩小资源范围")
        atomic_write_json(args.out / "run.json", {"config_sha256": config_hash, "configuration": configuration,
                                                   "max_resources": args.max_resources}, indent=2)
        manifest = {"schema_version": 1, "model_type": "prophet", "prophet_version": __version__,
                    "resource_type": args.resource_type, "level": args.level, "metric": args.metric,
                    "train_end_exclusive": args.train_end, "parameters": parameters, "seed": args.seed,
                    "source": {"raw": str(args.raw.resolve())}, "models": {}, "failures": [], "skipped": [],
                    "status": "running", "generated_at": pd.Timestamp.now(tz="UTC").isoformat()}
        atomic_write_json(args.out / "manifest.json", manifest, indent=2)
        trained = reused = processed = 0
        started = time.perf_counter()
        try:
            for identity, history in iter_histories(args, store, manifest["skipped"], manifest["failures"]):
                processed += 1
                key = model_key(**identity)
                key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
                checkpoint = args.out / "checkpoints" / (key_hash + ".json")
                data_hash = fingerprint(identity, history)
                try:
                    entry = reusable_entry(args.out, checkpoint, data_hash, config_hash) if resume else None
                    if entry is not None and any(entry.get(k) != v for k, v in identity.items()):
                        entry = None
                    if entry is not None:
                        reused += 1
                    else:
                        tick = time.perf_counter()
                        model = Prophet(stan_backend="CMDSTANPY", **parameters)
                        model.fit(pd.DataFrame({"ds": history.index, "y": history.to_numpy()}), seed=args.seed)
                        serialized = model_to_json(model)
                        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                        filename = f"models/{key_hash}-{digest}.json"
                        atomic_write_text(args.out / filename, serialized)
                        entry = {**identity, "file": filename, "sha256": digest,
                                 "data_sha256": data_hash, "config_sha256": config_hash,
                                 "train_start": history.index[0].isoformat(), "train_end": history.index[-1].isoformat(),
                                 "points": len(history), "step_seconds": (history.index[1]-history.index[0]).total_seconds(),
                                 "upper_bound": usage_forecast_upper_bound(history), "fit_seconds": time.perf_counter()-tick}
                        # 模型先落盘，再原子提交检查点；中断最多重做当前未提交的模型。
                        atomic_write_json(checkpoint, entry)
                        trained += 1
                        del model, serialized
                    manifest["models"][key] = entry
                except Exception as exc:
                    manifest["failures"].append({**identity, "error": str(exc)})
                if processed % 10 == 0:
                    print(f"已处理 {processed} 条，新增训练 {trained}，复用 {reused}，失败 {len(manifest['failures'])}", flush=True)
                del history
            manifest["status"] = "complete"
        finally:
            if manifest["status"] == "running":
                manifest["status"] = "interrupted"
            manifest.update(training_seconds=time.perf_counter()-started, trained=trained, reused=reused)
            # 总索引只在开始/结束写入，避免每条模型都重写增长中的大索引。
            atomic_write_json(args.out / "manifest.json", manifest, indent=2)
        return {"trained": trained, "reused": reused, "available": len(manifest["models"]),
                "failed": len(manifest["failures"]), "skipped": len(manifest["skipped"]),
                "training_seconds": manifest["training_seconds"]}
