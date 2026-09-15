"""批次级 LSTM 续训状态；测试评价不参与训练状态恢复。"""
import copy
import os
import random
import time

import numpy as np
import torch
from torch.utils.data import DataLoader

from training.lstm_data import SeriesSampler


def atomic_save(value, path):
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as output:
            torch.save(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def rng_state():
    state = np.random.get_state()
    return {"python": random.getstate(), "numpy": (state[0], state[1].tolist(), *state[2:]),
            "torch": torch.get_rng_state(), "cuda": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else []}


def restore_rng(state):
    random.setstate(state["python"])
    values = state["numpy"]
    np.random.set_state((values[0], np.asarray(values[1], dtype=np.uint32), *values[2:]))
    torch.set_rng_state(state["torch"])
    if state["cuda"]:
        torch.cuda.set_rng_state_all(state["cuda"])


def train_epochs(args, model, dataset, valid_loader, evaluate, config_hash, data_hash):
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    path = args.out / "resume.pt"
    state = {"format": 1, "epoch": 1, "next_batch": 0, "total": 0.0, "count": 0,
             "best": float("inf"), "best_state": None, "best_epoch": 0, "stale": 0, "history": [],
             "config_hash": config_hash, "data_hash": data_hash, "training_seconds": 0.0}
    if args.resume and path.exists():
        state = torch.load(path, map_location="cpu", weights_only=True)
        if state.get("format") != 1 or state["config_hash"] != config_hash or state["data_hash"] != data_hash:
            raise ValueError("LSTM 续训检查点与配置/数据不匹配")
        if args.epochs < state["epoch"]-1+int(state["next_batch"] > 0):
            raise ValueError("epochs 不能小于已经完成的轮数")
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        restore_rng(state["rng"])
        print(f"恢复第 {state['epoch']} 轮，下一个批次 {state['next_batch']}，已完成 {len(state['history'])} 轮", flush=True)
    started = time.perf_counter()
    previous_seconds = state["training_seconds"]

    def save():
        state.update(model=model.state_dict(), optimizer=optimizer.state_dict(), rng=rng_state(),
                     training_seconds=previous_seconds+time.perf_counter()-started)
        atomic_save(state, path)

    if not path.exists():
        save()
    while state["epoch"] <= args.epochs and state["stale"] < args.patience:
        epoch = state["epoch"]
        sampler = SeriesSampler(dataset.positions, args.seed, epoch,
                                start=min(len(dataset), state["next_batch"]*args.batch_size))
        loader = DataLoader(dataset, batch_size=args.batch_size, sampler=sampler,
                            generator=torch.Generator().manual_seed(args.seed+epoch))
        model.train()
        for inputs, targets in loader:
            optimizer.zero_grad()
            loss = torch.nn.functional.mse_loss(model(inputs.to(args.device)), targets.to(args.device))
            if not torch.isfinite(loss):
                raise ValueError("训练损失非有限值，请检查输入和学习率")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            state["total"] += float(loss.detach().cpu())*len(inputs)
            state["count"] += len(inputs)
            state["next_batch"] += 1
            if state["next_batch"] % args.checkpoint_every == 0:
                save()
                print(f"第 {epoch} 轮已保存 {state['next_batch']} 批次", flush=True)
        # 验证中断时从本轮训练结束继续，不必重跑本轮所有梯度更新。
        save()
        validation = evaluate(model, valid_loader, args.device)
        if not np.isfinite(validation):
            raise ValueError("验证误差非有限值")
        record = {"epoch": epoch, "train_normalized_mse": state["total"]/state["count"],
                  "validation_normalized_mse": validation}
        state["history"].append(record)
        print(record, flush=True)
        if validation < state["best"]:
            state.update(best=validation, best_epoch=epoch, stale=0,
                         best_state={k: value.detach().cpu().clone() for k, value in model.state_dict().items()})
        else:
            state["stale"] += 1
        state.update(epoch=epoch+1, next_batch=0, total=0.0, count=0)
        save()
    if state["best_state"] is None:
        raise ValueError("没有有效验证结果")
    return copy.deepcopy(state["best_state"]), state["best_epoch"], state["history"], state["training_seconds"]
