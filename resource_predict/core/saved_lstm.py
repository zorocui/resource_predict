"""只读加载离线 LSTM；每进程有界缓存，不创建优化器、不在线训练。"""
from collections import OrderedDict
import hashlib
import io
from pathlib import Path
import threading

import numpy as np
import pandas as pd

from resource_predict.core.forecasting import clip_usage_range, usage_forecast_upper_bound

_cache = OrderedDict()
_lock = threading.RLock()


def pin_checkpoint(path):
    """在任务开始固定模型文件版本；文件不可用时交由候选失败兜底。"""
    try:
        if not path:
            raise ValueError("未配置 lstm_model_path")
        file = Path(path).resolve()
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            for block in iter(lambda: stream.read(1024*1024), b""):
                digest.update(block)
        return {"path": str(file), "sha256": digest.hexdigest()}
    except (OSError, ValueError) as exc:
        return {"path": str(path), "error": str(exc)}


def _load(descriptor):
    if not descriptor or descriptor.get("error"):
        raise ValueError("LSTM 模型不可用: " + str((descriptor or {}).get("error", "未固定模型版本")))
    key = (descriptor["path"], descriptor["sha256"])
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        import torch
        from resource_predict.core.lstm_model import SharedLSTM

        data = Path(descriptor["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
            raise ValueError("本轮固定的 LSTM 文件已改变，拒绝混用模型版本")
        checkpoint = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
        if checkpoint.get("model_version") not in {"shared-lstm-experiment-v2", "shared-lstm-experiment-v3"}:
            raise ValueError("仅支持训练产物 model.pt v2/v3，不支持 resume.pt")
        architecture = checkpoint["architecture"]
        lookback, horizon = int(checkpoint["lookback"]), int(architecture["horizon"])
        if min(lookback, horizon) <= 0:
            raise ValueError("LSTM 窗口配置无效")
        boundaries = checkpoint["boundaries"]
        train_end, selection_end = pd.Timestamp(boundaries["train_end_exclusive"]), pd.Timestamp(boundaries["validation_end_exclusive"])
        step = float(boundaries["step_seconds"])
        if pd.isna(train_end) or pd.isna(selection_end) or not train_end < selection_end or not np.isfinite(step) or step <= 0:
            raise ValueError("LSTM 训练/选型时间边界或采样间隔无效")
        scalers = {}
        for row in checkpoint["scalers"]:
            identity = (str(row["resource_id"]), str(row["container"]), str(row["metric"]))
            mean, scale = float(row["mean"]), float(row["scale"])
            if identity in scalers or not np.isfinite([mean, scale]).all() or scale <= 0:
                raise ValueError("LSTM 归一化参数重复或无效")
            scalers[identity] = (mean, scale)
        torch.set_num_threads(1)
        model = SharedLSTM(**architecture)
        model.load_state_dict(checkpoint["state_dict"], strict=True)
        model.eval()
        result = {"model": model, "scalers": scalers, "config": checkpoint["config"], "lookback": lookback,
                  "horizon": horizon, "step": step, "train_end": train_end, "selection_end": selection_end,
                  "version": checkpoint["model_version"]}
        _cache[key] = result
        if len(_cache) > 2:
            _cache.popitem(last=False)
        return result


def forecast_saved_lstm(history, index, identity, descriptor, max_age_hours):
    model = _load(descriptor)
    if not identity:
        raise ValueError("LSTM 缺少资源/容器/指标身份")
    level = "container" if identity.get("container") else "resource"
    if identity["resource_type"] != model["config"].get("resource_type") or level != model["config"].get("level"):
        raise ValueError("LSTM 资源类型或容器/资源层级不匹配")
    key = tuple(str(identity[name]) for name in ("resource_id", "container", "metric"))
    if key not in model["scalers"]:
        raise ValueError("LSTM 没有该资源/容器/指标的归一化参数")
    if not len(index) or len(index) > model["horizon"]:
        raise ValueError("预测/验证窗口超过 LSTM 已训练的输出长度")
    # 最佳 epoch 已使用离线验证标签：不仅训练段，离线验证段也不能拿来做在线选型证据。
    if index[0] < model["selection_end"]:
        raise ValueError("LSTM 验证/测试窗口与离线训练或选型数据重叠")
    if len(history) < model["lookback"]:
        raise ValueError("LSTM 输入历史不足 lookback")
    step_ns = int(model["step"] * 1e9)
    tail = history.iloc[-model["lookback"]:]
    if (not np.all(np.diff(tail.index.asi8) == step_ns)
            or not np.all(np.diff(index.asi8) == step_ns)
            or index[0].value-tail.index[-1].value != step_ns):
        raise ValueError("LSTM 时间网格或采样间隔与训练模型不匹配")
    age = max(0.0, (history.index[-1]-model["train_end"]).total_seconds())
    if max_age_hours > 0 and age > max_age_hours*3600:
        raise ValueError("LSTM 模型超过允许训练滞后时间，请离线重训")
    mean, scale = model["scalers"][key]
    values = ((tail.to_numpy(dtype=float)-mean)/scale).astype(np.float32)
    if not np.isfinite(values).all():
        raise ValueError("LSTM 输入含非有限值")
    import torch
    with torch.inference_mode():
        predicted = model["model"](torch.from_numpy(values[None, :, None])).cpu().numpy()[0, :len(index)]
    predicted = predicted*scale+mean
    if not np.isfinite(predicted).all():
        raise ValueError("LSTM 输出含非有限值")
    result = clip_usage_range(pd.Series(predicted, index=index), upper=usage_forecast_upper_bound(history))
    metadata = {"sha256": descriptor["sha256"], "model_version": model["version"], "inference_only": True,
                "train_end_exclusive_ms": model["train_end"].value // 1_000_000,
                "selection_end_exclusive_ms": model["selection_end"].value // 1_000_000,
                "model_age_seconds": age, "metric_semantics_verified": False}
    return result, metadata
