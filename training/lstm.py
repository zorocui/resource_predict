"""共享 LSTM 离线实验：python -m training.lstm --help。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import time

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader

from resource_predict.core.lstm_model import SharedLSTM
from resource_predict.core.forecasting import clip_usage_range, usage_forecast_upper_bound
from resource_predict.data.io import atomic_write_json
from resource_predict.pipeline.forecasting import forecast_by_method
from resource_predict.pipeline.series_utils import compute_metrics
from training.lstm_data import DiskValues, Positions, Windows, file_hash, prepare
from training.lstm_checkpoint import atomic_save, train_epochs
from training.prophet_training import training_lock


def evaluate(model, loader, device):
    model.eval()
    total, points = 0.0, 0
    with torch.inference_mode():
        for inputs, targets in loader:
            predicted = model(inputs.to(device))
            total += float(torch.sum((predicted - targets.to(device)) ** 2).cpu())
            points += targets.numel()
    return total / points


def run(args):
    if args.out.exists() and not args.resume:
        raise ValueError("输出目录已存在；续训请添加 --resume，新实验请指定新 --out")
    if args.resume and not (args.out / "run.json").is_file():
        raise ValueError("缺少 run.json，旧版 model.pt 不能作为续训检查点")
    if args.prophet_models:
        args.baselines = [m for m in args.baselines if m not in {"prophet", "prophet_saved"}] + ["prophet_saved"]
    config = {k: str(v.resolve()) if isinstance(v, Path) else v for k, v in vars(args).items()
              if k not in {"out", "resume", "epochs", "checkpoint_every"}}
    config.update(stream_version=1, torch_version=str(torch.__version__))
    if args.prophet_models:
        config["prophet_manifest_sha256"] = file_hash(args.prophet_models / "manifest.json")
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    raw_hash = file_hash(args.raw / "raw_index.json")
    args.out.mkdir(parents=True, exist_ok=args.resume)
    with training_lock(args.out):
        if args.resume:
            previous = json.loads((args.out / "run.json").read_text(encoding="utf-8"))
            if previous["config_hash"] != config_hash:
                raise ValueError("LSTM 续训参数或运行环境不匹配，请使用原参数或新的输出目录")
            if previous["raw_index_sha256"] != raw_hash:
                raise ValueError("raw 数据索引已变化，不能续训同一个 LSTM；请使用原快照或新输出目录")
        else:
            atomic_write_json(args.out / "run.json", {"config_hash": config_hash, "configuration": config,
                                                       "raw_index_sha256": raw_hash}, indent=2)
        atomic_write_json(args.out / "status.json", {"status": "running"})
        try:
            result = _run(args, config_hash)
        except BaseException:
            atomic_write_json(args.out / "status.json", {"status": "interrupted"})
            raise
        atomic_write_json(args.out / "status.json", {"status": "complete"})
        return result


def _run(args, config_hash):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(args.threads)
    torch.use_deterministic_algorithms(True)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA 不可用，请使用 --device cpu")
    started = time.perf_counter()
    prepared = prepare(args)
    items, skipped, source = prepared["series"], prepared["skipped"], prepared["source"]
    lookback, horizon, boundaries = prepared["lookback"], prepared["horizon"], prepared["boundaries"]
    positions = {phase: Positions(items, phase) for phase in ("train", "validation", "test")}
    disk_values = DiskValues(args.out / "data-cache", items)
    prophet_bank = None
    if args.prophet_models:
        from training.prophet import SavedProphetModels
        prophet_bank = SavedProphetModels(args.prophet_models, args.resource_type, args.level)
        for item in items:
            entry = prophet_bank.entry(item["resource_id"], item["container"], item["metric"])
            if pd.Timestamp(entry["train_end"]) >= pd.Timestamp(boundaries["train_end_exclusive"]):
                raise ValueError("已保存 Prophet 超过 LSTM 训练截止点；请用相同 --train-end 重新训练基线")
        args.baselines = [m for m in args.baselines if m not in {"prophet", "prophet_saved"}] + ["prophet_saved"]
    datasets = {phase: Windows(disk_values, pos, lookback, horizon) for phase, pos in positions.items()}
    loaders = {phase: DataLoader(dataset, batch_size=args.batch_size,
                                 generator=torch.Generator().manual_seed(args.seed))
               for phase, dataset in datasets.items() if phase != "train"}
    architecture = {"hidden_size": args.hidden_size, "layers": args.layers,
                    "dropout": args.dropout, "horizon": horizon}
    model = SharedLSTM(**architecture).to(args.device)
    print(json.dumps({"series": len(items), "windows": {p: len(v) for p, v in positions.items()},
                      "boundaries": boundaries}, ensure_ascii=False), flush=True)
    best_state, best_epoch, history, training_seconds = train_epochs(
        args, model, datasets["train"], loaders["validation"], evaluate, config_hash,
        file_hash(args.out / "data-cache" / "index.json"))
    model.load_state_dict(best_state)
    model.eval()
    config = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    scalers = [{"resource_id": item["resource_id"], "container": item["container"], "metric": item["metric"],
                "mean": item["mean"], "scale": item["scale"]} for item in items]
    checkpoint = {"model_version": "shared-lstm-experiment-v3", "architecture": architecture,
                  "lookback": lookback, "state_dict": {k: v.cpu() for k, v in best_state.items()},
                  "scalers": scalers, "boundaries": boundaries, "config": config,
                  "source": source, "best_epoch": best_epoch}
    atomic_save(checkpoint, args.out / "model.pt")
    # 测试阶段每个预测起点只读取此前观测；所有模型使用相同测试标签。
    rows, inference_seconds = [], 0.0
    offset = 0
    with (args.out / "predictions.jsonl").open("w", encoding="utf-8") as curves:
        with torch.inference_mode():
            for inputs, _ in loaders["test"]:
                tick = time.perf_counter()
                predictions = model(inputs.to(args.device)).cpu().numpy()
                inference_seconds += time.perf_counter() - tick
                for values in predictions:
                    series_index, origin = positions["test"][offset]
                    offset += 1
                    item = items[series_index]
                    series = disk_values.series(series_index, boundaries["step_seconds"])
                    observed = series.iloc[:origin]
                    actual = series.iloc[origin:origin+horizon]
                    predicted = clip_usage_range(pd.Series(values * item["scale"] + item["mean"],
                                                           index=actual.index),
                                                  upper=usage_forecast_upper_bound(observed))
                    identity = {"resource_id": item["resource_id"], "container": item["container"],
                                "metric": item["metric"], "window_start": actual.index[0].isoformat(),
                                "window_end": actual.index[-1].isoformat()}
                    methods = {"lstm": predicted}
                    timings = {}
                    for method in args.baselines:
                        tick = time.perf_counter()
                        try:
                            if method == "prophet_saved":
                                result = prophet_bank.predict(item["resource_id"], item["container"], item["metric"], actual.index)
                            else:
                                result = forecast_by_method(method, observed, horizon).yhat
                            if len(result) != horizon or not np.isfinite(result.to_numpy()).all():
                                raise ValueError("基线预测非有限值或长度不匹配")
                            methods[method] = result.set_axis(actual.index)
                        except Exception as exc:
                            rows.append({**identity, "model": method, "status": "failed", "error": str(exc)})
                        timings[method] = time.perf_counter() - tick
                    for method, predicted in methods.items():
                        errors = actual.to_numpy() - predicted.to_numpy()
                        rows.append({**identity, "model": method, "status": "ok",
                                     **compute_metrics(actual, predicted),
                                     "mean_underprediction": float(np.maximum(errors, 0).mean()),
                                     "peak_underprediction": max(0.0, float(actual.max()-predicted.max())),
                                     "wall_seconds": timings.get(method),
                                     **({"model_train_end": prophet_bank.entry(item["resource_id"], item["container"], item["metric"])["train_end"]}
                                        if method == "prophet_saved" else {})})
                    curves.write(json.dumps({**identity, "actual": actual.tolist(),
                                             "predictions": {m: p.tolist() for m, p in methods.items()}},
                                            ensure_ascii=False, allow_nan=False) + "\n")
    summary = summarize(rows, ["lstm", *args.baselines])
    metrics = sorted({item["metric"] for item in items})
    summary_by_metric = {metric: summarize([r for r in rows if r["metric"] == metric],
                                            ["lstm", *args.baselines]) for metric in metrics}
    report = {"config": config, "source": source, "boundaries": boundaries, "series": len(items),
              "series_by_metric": {metric: sum(item["metric"] == metric for item in items) for metric in metrics},
              "window_counts": {p: len(v) for p, v in positions.items()}, "skipped": skipped,
              "torch_version": str(torch.__version__), "best_epoch": best_epoch, "history": history,
              "selection_metric": "validation_normalized_mse", "summary": summary,
              "summary_by_metric": summary_by_metric,
              "training_seconds": training_seconds, "lstm_batch_inference_seconds": inference_seconds,
              "total_seconds": time.perf_counter()-started,
              "evaluation": "共同截止点；所有指标共享网络，逐序列归一化；测试滚动起点使用此前观测，网络权重和归一化固定；窗口误差等权均值，跨指标总均值仅供诊断"}
    atomic_write_json(args.out / "report.json", report, indent=2)
    atomic_write_json(args.out / "forecast_error_report.json", {"window_role": "independent_test", "records": rows}, indent=2)
    print(json.dumps(summary_by_metric, ensure_ascii=False, indent=2), flush=True)
    return report


def summarize(rows, methods):
    summary = {}
    for method in methods:
        valid = [r for r in rows if r["model"] == method and r["status"] == "ok"]
        summary[method] = {"successful_windows": len(valid),
                           "failed_windows": sum(r["model"] == method and r["status"] == "failed" for r in rows)}
        for key in ("rmse", "mae", "mape", "p95_error", "mean_underprediction", "peak_underprediction"):
            summary[method]["mean_window_" + key] = float(np.mean([r[key] for r in valid])) if valid else None
    return summary


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--raw", type=Path, required=True, help="包含 raw_index.json 和 raw/ 的本地目录")
    command.add_argument("--out", type=Path, required=True, help="新的实验输出目录")
    command.add_argument("--resource-type", choices=["openstack_vm", "k8s_workload"], required=True)
    command.add_argument("--metric", default="all", help="默认 all：所有指标共享一个 LSTM；也可指定单一指标")
    command.add_argument("--level", choices=["resource", "container"], default="resource")
    command.add_argument("--lookback", default="24h")
    command.add_argument("--horizon", default="24h")
    command.add_argument("--validation-duration", default="24h")
    command.add_argument("--test-duration", default="24h")
    command.add_argument("--train-end", help="所有资源统一的训练截止时间（不含）")
    command.add_argument("--validation-end", help="所有资源统一的验证截止时间（不含）")
    command.add_argument("--max-resources", type=int, default=100, help="固定种子抽样；0 为全部")
    command.add_argument("--stride", type=int, default=6, help="训练窗口起点间隔（点）；评价窗口不重叠")
    command.add_argument("--hidden-size", type=int, default=32)
    command.add_argument("--layers", type=int, default=1)
    command.add_argument("--dropout", type=float, default=0.1)
    command.add_argument("--learning-rate", type=float, default=0.001)
    command.add_argument("--batch-size", type=int, default=64)
    command.add_argument("--epochs", type=int, default=20)
    command.add_argument("--patience", type=int, default=3)
    command.add_argument("--resume", action="store_true", help="同一输出目录恢复训练状态；数据及关键参数必须一致")
    command.add_argument("--checkpoint-every", type=int, default=1000, help="每多少个训练批次原子保存续训状态；每轮结束也保存")
    command.add_argument("--threads", type=int, default=2)
    command.add_argument("--seed", type=int, default=42)
    command.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    command.add_argument("--baselines", nargs="+", choices=["seasonal_naive", "rolling_mean", "prophet"],
                         default=["seasonal_naive", "rolling_mean", "prophet"])
    command.add_argument("--prophet-models", type=Path,
                         help="加载已保存 Prophet 作为 prophet_saved 基线，替换即时拟合的 prophet；不自动重训")
    return command


def main():
    command = parser()
    args = command.parse_args()
    if (any(getattr(args, key) <= 0 for key in ("stride", "hidden_size", "layers", "batch_size",
                                               "epochs", "patience", "threads", "checkpoint_every"))
            or args.max_resources < 0 or not 0 <= args.dropout < 1
            or not np.isfinite(args.learning_rate) or args.learning_rate <= 0):
        command.error("整数训练参数必须为正；max-resources >= 0；0 <= dropout < 1；learning-rate > 0")
    if any(pd.Timedelta(value) <= pd.Timedelta(0) for value in
           (args.lookback, args.horizon, args.validation_duration, args.test_duration)):
        command.error("所有时长必须为正")
    run(args)


if __name__ == "__main__":
    main()
