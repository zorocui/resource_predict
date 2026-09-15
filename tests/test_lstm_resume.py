"""磁盘窗口读取、确定性批次恢复和输入一致性检查。"""
import json

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from training.lstm import parser, run
from training.lstm_data import DiskValues, Positions, SeriesSampler, prepare
import training.lstm_checkpoint as checkpoints


def arguments(tmp_path, output="result"):
    raw = tmp_path / "raw"
    if not raw.exists():
        times = pd.date_range("2026-01-01", periods=192, freq="h")
        values = .3+.05*np.sin(np.arange(192)*2*np.pi/24)
        write_raw_resource_dataset(raw, [
            {"resource_id": f"vm-{i}", "resource_type": "openstack_vm", "spec": {},
             "cpu": pd.Series(values+i*.03, index=times),
             "memory": pd.Series(values*2+i*.04, index=times),
             "disk": pd.Series(values*.5+i*.02, index=times)} for i in range(2)], freq="h")
    return parser().parse_args(["--raw", str(raw), "--out", str(tmp_path / output),
                               "--resource-type", "openstack_vm", "--epochs", "2", "--hidden-size", "4",
                               "--batch-size", "7", "--dropout", "0.2", "--checkpoint-every", "1",
                               "--baselines", "seasonal_naive", "rolling_mean"])


def test_mid_epoch_resume_matches_uninterrupted(tmp_path, monkeypatch):
    baseline = arguments(tmp_path, "baseline")
    expected = run(baseline)
    interrupted = arguments(tmp_path, "interrupted")
    original_save = checkpoints.atomic_save

    def stop_after_checkpoint(value, path):
        original_save(value, path)
        if path.name == "resume.pt" and value["epoch"] == 1 and value["next_batch"] == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(checkpoints, "atomic_save", stop_after_checkpoint)
    with pytest.raises(KeyboardInterrupt):
        run(interrupted)
    saved = torch.load(interrupted.out / "resume.pt", weights_only=True)
    assert saved["next_batch"] == 2 and saved["optimizer"]["state"] and saved["rng"]
    assert json.loads((interrupted.out / "status.json").read_text())["status"] == "interrupted"
    monkeypatch.setattr(checkpoints, "atomic_save", original_save)
    interrupted.resume = True
    actual = run(interrupted)
    assert actual["history"] == expected["history"]
    for filename, key in (("model.pt", "state_dict"), ("resume.pt", "model")):
        first = torch.load(baseline.out / filename, weights_only=True)[key]
        second = torch.load(interrupted.out / filename, weights_only=True)[key]
        assert all(torch.equal(first[name], second[name]) for name in first)
    # 已完成训练再次 resume 只重做评价，不进行梯度更新。
    monkeypatch.setattr(torch.optim.Adam, "step", lambda *_args, **_kwargs: pytest.fail("不应继续更新"))
    assert run(interrupted)["history"] == expected["history"]


def test_cache_is_streamed_and_positions_are_compact(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    args.out.mkdir()
    reads, writes = [], []
    original_get, original_save = RawResourceStore.get, np.save

    def get(store, rid):
        reads.append(rid)
        return original_get(store, rid)

    def save(*values, **kwargs):
        writes.append(len(reads))
        return original_save(*values, **kwargs)

    monkeypatch.setattr(RawResourceStore, "get", get)
    monkeypatch.setattr(np, "save", save)
    result = prepare(args)
    assert writes[:3] == [1, 1, 1] and writes[3:] == [2, 2, 2]
    positions = Positions(result["series"], "train")
    assert len(positions.ranges) == 6 and len(positions) > 6
    order = list(SeriesSampler(positions, 42, 1))
    assert sorted(order) == list(range(len(positions)))
    assert list(SeriesSampler(positions, 42, 1, start=14)) == order[14:]
    disk = DiskValues(args.out / "data-cache", result["series"], capacity=2)
    for i in range(6):
        assert isinstance(disk[i], np.memmap)
        assert len(disk.cache) <= 2
    cutoff = pd.Timestamp(result["boundaries"]["train_end_exclusive"])
    valid_end = pd.Timestamp(result["boundaries"]["validation_end_exclusive"])
    step = pd.Timedelta(seconds=result["boundaries"]["step_seconds"])
    for phase in ("train", "validation", "test"):
        for i, origin in Positions(result["series"], phase):
            start = pd.Timestamp(result["series"][i]["start"])+origin*step
            last = start+(result["horizon"]-1)*step
            if phase == "train":
                assert last < cutoff
            elif phase == "validation":
                assert cutoff <= start and last < valid_end
            else:
                assert start >= valid_end
    for i, meta in enumerate(result["series"]):
        count = int((cutoff-pd.Timestamp(meta["start"]))/step)
        assert meta["mean"] == float(disk[i][:count].mean())
    huge = Positions([{"ranges": {"train": [0, 10_000_000, 1]}}], "train")
    assert len(huge) == 10_000_000 and len(huge.ranges) == len(huge.ends) == 1


def test_resume_rejects_config_input_and_cache_changes(tmp_path):
    args = arguments(tmp_path)
    args.epochs = 1
    run(args)
    args.resume = True
    args.batch_size += 1
    with pytest.raises(ValueError, match="参数"):
        run(args)
    args.batch_size -= 1
    index = args.raw / "raw_index.json"
    previous = index.read_bytes()
    index.write_bytes(previous+b"\n")
    with pytest.raises(ValueError, match="raw 数据索引已变化"):
        run(args)
    index.write_bytes(previous)
    path = next((args.out / "data-cache").glob("*.npy"))
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="缓存损坏"):
        run(args)


def test_validation_interruption_does_not_repeat_training(tmp_path, monkeypatch):
    import training.lstm as lstm
    args = arguments(tmp_path)
    args.epochs = 1
    original_evaluate = lstm.evaluate

    def stop_validation(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(lstm, "evaluate", stop_validation)
    with pytest.raises(KeyboardInterrupt):
        run(args)
    state = torch.load(args.out / "resume.pt", weights_only=True)
    assert state["count"] > 0 and state["epoch"] == 1 and not state["history"]
    monkeypatch.setattr(lstm, "evaluate", original_evaluate)
    monkeypatch.setattr(torch.optim.Adam, "step", lambda *_args, **_kwargs: pytest.fail("验证恢复不应重复训练"))
    args.resume = True
    assert run(args)["best_epoch"] == 1
