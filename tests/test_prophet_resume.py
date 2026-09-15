"""真实小模型验证流式读取、异常中断和有条件复用。"""
import json

import numpy as np
import pandas as pd
import pytest

from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from training.prophet import SavedProphetModels, parser, train
from training.prophet_training import training_lock


def make_args(tmp_path):
    times = pd.date_range("2026-01-01", periods=96, freq="h")
    series = pd.Series(.4 + .1 * np.sin(np.arange(96)*2*np.pi/24), index=times)
    items = [{"resource_id": f"vm-{i}", "resource_type": "openstack_vm", "spec": {},
              "cpu": series.copy(), "memory": series.copy(), "disk": series.copy()} for i in range(3)]
    raw = tmp_path / "raw"
    write_raw_resource_dataset(raw, items, freq="h")
    args = parser().parse_args(["train", "--raw", str(raw), "--out", str(tmp_path / "models"),
                               "--resource-type", "openstack_vm", "--metric", "cpu", "--max-resources", "0",
                               "--train-end", "2026-01-04"])
    return args, items


def test_stream_interrupt_resume_and_data_change(tmp_path, monkeypatch):
    prophet = pytest.importorskip("prophet")
    args, items = make_args(tmp_path)
    original_fit, original_get = prophet.Prophet.fit, RawResourceStore.get
    reads, fits = [], []

    def counted_get(store, rid):
        reads.append(rid)
        return original_get(store, rid)

    def interrupt_fit(model, frame, **kwargs):
        fits.append(len(frame))
        # 每次训练前仅读取到了当前资源，没有预加载下一资源。
        assert len(reads) == len(fits)
        if len(fits) == 2:
            raise KeyboardInterrupt()
        return original_fit(model, frame, **kwargs)

    monkeypatch.setattr(RawResourceStore, "get", counted_get)
    monkeypatch.setattr(prophet.Prophet, "fit", interrupt_fit)
    with pytest.raises(KeyboardInterrupt):
        train(args)
    assert len(list((args.out / "checkpoints").glob("*.json"))) == 1
    with pytest.raises(ValueError, match="尚未完成"):
        SavedProphetModels(args.out)
    # 模拟未能最终写清单的硬中断：续训仅依赖 run.json 和逐模型检查点。
    (args.out / "manifest.json").unlink()
    args.resume = True
    fits.clear()

    def counted_fit(model, frame, **kwargs):
        fits.append(len(frame))
        return original_fit(model, frame, **kwargs)

    monkeypatch.setattr(prophet.Prophet, "fit", counted_fit)
    report = train(args)
    assert report["trained"] == 2 and report["reused"] == 1 and report["available"] == 3
    assert len(fits) == 2
    fits.clear()
    again = train(args)
    assert again["trained"] == 0 and again["reused"] == 3 and not fits
    assert len(SavedProphetModels(args.out).entries) == 3
    # 修改一条训练序列：只有这一条重训，其余原样复用。
    items[0]["cpu"] = items[0]["cpu"] + .02
    write_raw_resource_dataset(args.raw, items, freq="h")
    changed = train(args)
    assert changed["trained"] == 1 and changed["reused"] == 2 and len(fits) == 1
    fits.clear()
    items[1]["cpu"].iloc[-24:] += .1
    write_raw_resource_dataset(args.raw, items, freq="h")
    later = train(args)
    assert later["trained"] == 0 and later["reused"] == 3 and not fits
    manifest = json.loads((args.out / "manifest.json").read_text(encoding="utf-8"))
    broken = next(iter(manifest["models"].values()))
    (args.out / broken["file"]).write_text("{}", encoding="utf-8")
    repaired = train(args)
    assert repaired["trained"] == 1 and repaired["reused"] == 2 and len(fits) == 1
    args.seed += 1
    with pytest.raises(ValueError, match="配置"):
        train(args)


def test_lock_and_resource_range_expansion(tmp_path):
    pytest.importorskip("prophet")
    args, _ = make_args(tmp_path)
    args.max_resources = 1
    first = train(args)
    assert first["trained"] == 1
    args.resume = True
    with training_lock(args.out):
        with pytest.raises(ValueError, match="已有训练进程"):
            train(args)
    args.max_resources = 0
    expanded = train(args)
    assert expanded["trained"] == 2 and expanded["reused"] == 1
    args.max_resources = 1
    with pytest.raises(ValueError, match="不能缩小"):
        train(args)
