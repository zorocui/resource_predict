"""共享 LSTM 离线实验：python -m training.lstm --help。"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import random
import time

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset

from resource_predict.core.forecasting import clip_usage_range, usage_forecast_upper_bound
from resource_predict.data.io import atomic_write_json
from resource_predict.pipeline.forecasting import forecast_by_method
from resource_predict.pipeline.series_utils import compute_metrics
from training.data import load_series, split_windows


class Windows(Dataset):
    """仅存每条原序列和窗口位置，按批次生成输入。"""

    def __init__(self, values, positions, lookback, horizon):
        self.values = values
        self.positions = positions
        self.lookback, self.horizon = lookback, horizon

    def __len__(self):
        return len(self.positions)

    def __getitem__(self, index):
        series, origin = self.positions[index]
        values = self.values[series]
        return (torch.from_numpy(values[origin-self.lookback:origin, None]),
                torch.from_numpy(values[origin:origin+self.horizon]))


class SharedLSTM(nn.Module):
    def __init__(self, hidden_size, layers, dropout, horizon):
        super().__init__()
        self.encoder = nn.LSTM(1, hidden_size, num_layers=layers, batch_first=True,
                               dropout=dropout if layers > 1 else 0.0)
        self.head = nn.Sequential(nn.Dropout(dropout), nn.Linear(hidden_size, horizon))

    def forward(self, values):
        _, (hidden, _) = self.encoder(values)
        return self.head(hidden[-1])


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
    if args.out.exists():
        raise ValueError("输出目录已存在，请指定新的 --out，避免覆盖实验")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(args.threads)
    torch.use_deterministic_algorithms(True)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA 不可用，请使用 --device cpu")
    started = time.perf_counter()
    items, skipped, source = load_series(args.raw, args.resource_type, args.metric,
                                         args.level, args.max_resources, args.seed)
    step = (items[0].series.index[1] - items[0].series.index[0]).total_seconds()
    sizes = []
    for duration in (args.lookback, args.horizon):
        count = pd.Timedelta(duration).total_seconds() / step
        if count < 1 or not count.is_integer():
            raise ValueError("lookback/horizon 必须是采样间隔的正整数倍")
        sizes.append(int(count))
    lookback, horizon = sizes
    items, positions, short, boundaries = split_windows(
        items, lookback, horizon, args.stride, args.validation_duration, args.test_duration,
        args.train_end, args.validation_end)
    skipped.extend(short)
    normalized = [((item.series.to_numpy(dtype=np.float64) - item.mean) / item.scale)
                  .astype(np.float32) for item in items]
    loaders = {phase: DataLoader(Windows(normalized, pos, lookback, horizon),
                                 batch_size=args.batch_size, shuffle=phase == "train")
               for phase, pos in positions.items()}
    architecture = {"hidden_size": args.hidden_size, "layers": args.layers,
                    "dropout": args.dropout, "horizon": horizon}
    model = SharedLSTM(**architecture).to(args.device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    best, best_state, best_epoch, stale, history = float("inf"), None, 0, 0, []
    args.out.mkdir(parents=True)
    print(json.dumps({"series": len(items), "windows": {p: len(v) for p, v in positions.items()},
                      "boundaries": boundaries}, ensure_ascii=False), flush=True)
    training_started = time.perf_counter()
    for epoch in range(1, args.epochs + 1):
        model.train()
        total, count = 0.0, 0
        for inputs, targets in loaders["train"]:
            optimizer.zero_grad()
            loss = nn.functional.mse_loss(model(inputs.to(args.device)), targets.to(args.device))
            if not torch.isfinite(loss):
                raise ValueError("训练损失非有限值，请检查输入和学习率")
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total += float(loss.detach().cpu()) * len(inputs)
            count += len(inputs)
        validation = evaluate(model, loaders["validation"], args.device)
        history.append({"epoch": epoch, "train_normalized_mse": total / count,
                        "validation_normalized_mse": validation})
        print(json.dumps(history[-1]), flush=True)
        if np.isfinite(validation) and validation < best:
            best, best_epoch, stale = validation, epoch, 0
            best_state = copy.deepcopy(model.state_dict())
        else:
            stale += 1
        if stale >= args.patience:
            break
    training_seconds = time.perf_counter() - training_started
    if best_state is None:
        raise ValueError("没有有效验证结果，未保存模型")
    model.load_state_dict(best_state)
    model.eval()
    config = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    scalers = [{"resource_id": item.resource_id, "container": item.container,
                "mean": item.mean, "scale": item.scale} for item in items]
    checkpoint = {"model_version": "shared-lstm-experiment-v1", "architecture": architecture,
                  "lookback": lookback, "state_dict": {k: v.cpu() for k, v in best_state.items()},
                  "scalers": scalers, "boundaries": boundaries, "config": config,
                  "source": source, "best_epoch": best_epoch}
    torch.save(checkpoint, args.out / "model.pt")
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
                    observed = item.series.iloc[:origin]
                    actual = item.series.iloc[origin:origin+horizon]
                    predicted = clip_usage_range(pd.Series(values * item.scale + item.mean,
                                                           index=actual.index),
                                                  upper=usage_forecast_upper_bound(observed))
                    identity = {"resource_id": item.resource_id, "container": item.container,
                                "metric": args.metric, "window_start": actual.index[0].isoformat(),
                                "window_end": actual.index[-1].isoformat()}
                    methods = {"lstm": predicted}
                    timings = {}
                    for method in args.baselines:
                        tick = time.perf_counter()
                        try:
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
                                     "wall_seconds": timings.get(method)})
                    curves.write(json.dumps({**identity, "actual": actual.tolist(),
                                             "predictions": {m: p.tolist() for m, p in methods.items()}},
                                            ensure_ascii=False, allow_nan=False) + "\n")
    summary = {}
    for method in ["lstm", *args.baselines]:
        valid = [r for r in rows if r["model"] == method and r["status"] == "ok"]
        summary[method] = {"successful_windows": len(valid),
                           "failed_windows": sum(r["model"] == method and r["status"] == "failed" for r in rows)}
        for key in ("rmse", "mae", "mape", "p95_error", "mean_underprediction", "peak_underprediction"):
            summary[method]["mean_window_" + key] = float(np.mean([r[key] for r in valid])) if valid else None
    report = {"config": config, "source": source, "boundaries": boundaries, "series": len(items),
              "window_counts": {p: len(v) for p, v in positions.items()}, "skipped": skipped,
              "torch_version": str(torch.__version__), "best_epoch": best_epoch, "history": history,
              "selection_metric": "validation_normalized_mse", "summary": summary,
              "training_seconds": training_seconds, "lstm_batch_inference_seconds": inference_seconds,
              "total_seconds": time.perf_counter()-started,
              "evaluation": "共同截止点；测试滚动起点使用此前观测，网络权重和归一化固定；窗口误差等权均值"}
    atomic_write_json(args.out / "report.json", report, indent=2)
    atomic_write_json(args.out / "forecast_error_report.json", {"window_role": "independent_test", "records": rows}, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return report


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--raw", type=Path, required=True, help="包含 raw_index.json 和 raw/ 的本地目录")
    command.add_argument("--out", type=Path, required=True, help="新的实验输出目录")
    command.add_argument("--resource-type", choices=["openstack_vm", "k8s_workload"], required=True)
    command.add_argument("--metric", required=True)
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
    command.add_argument("--threads", type=int, default=2)
    command.add_argument("--seed", type=int, default=42)
    command.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    command.add_argument("--baselines", nargs="+", choices=["seasonal_naive", "rolling_mean", "prophet"],
                         default=["seasonal_naive", "rolling_mean", "prophet"])
    return command


def main():
    command = parser()
    args = command.parse_args()
    if (any(getattr(args, key) <= 0 for key in ("stride", "hidden_size", "layers", "batch_size",
                                               "epochs", "patience", "threads"))
            or args.max_resources < 0 or not 0 <= args.dropout < 1
            or not np.isfinite(args.learning_rate) or args.learning_rate <= 0):
        command.error("整数训练参数必须为正；max-resources >= 0；0 <= dropout < 1；learning-rate > 0")
    if any(pd.Timedelta(value) <= pd.Timedelta(0) for value in
           (args.lookback, args.horizon, args.validation_duration, args.test_duration)):
        command.error("所有时长必须为正")
    run(args)


if __name__ == "__main__":
    main()
