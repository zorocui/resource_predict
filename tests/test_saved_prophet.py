"""序列身份、序列化复用、无 fit 推理与训练/测试边界。"""
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from resource_predict.data.raw_store import write_raw_resource_dataset
from training.prophet import SavedProphetModels, parser, predict, train


@pytest.fixture
def saved(tmp_path):
    pytest.importorskip("prophet")
    index = pd.date_range("2026-01-01", periods=24*8, freq="h")
    series = pd.Series(.4 + .08 * np.sin(np.arange(len(index))*2*np.pi/24), index=index)
    raw = tmp_path / "raw"
    write_raw_resource_dataset(raw, [{"resource_id": "w", "resource_type": "k8s_workload",
                                      "spec": {"containers": {}},
                                      **{k: series for k in ("cpu_request", "cpu_limit", "memory_request", "memory_limit")},
                                      "container_metrics": {"app": {"cpu_request": series}}}], freq="h")
    args = parser().parse_args(["train", "--raw", str(raw), "--out", str(tmp_path / "models"),
                               "--resource-type", "k8s_workload", "--level", "container",
                               "--metric", "cpu_request", "--train-end", "2026-01-07"])
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.rglob("*.json")}
    report = train(args)
    assert report["trained"] == 1 and report["failed"] == 0
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    return args.out, raw


def test_reload_and_predict_never_fit(saved, tmp_path, monkeypatch):
    from prophet import Prophet
    directory, raw = saved
    monkeypatch.setattr(Prophet, "fit", lambda *_args, **_kwargs: pytest.fail("推理不允许调用 fit"))
    times = pd.date_range("2026-01-09", periods=24, freq="h")
    bank = SavedProphetModels(directory, "k8s_workload", "container")
    first = bank.predict("w", "app", "cpu_request", times)
    second = SavedProphetModels(directory).predict("w", "app", "cpu_request", times)
    np.testing.assert_allclose(first, second)
    args = parser().parse_args(["predict", "--models", str(directory), "--raw", str(raw),
                               "--out", str(tmp_path / "predictions")])
    report = predict(args)
    assert report["fit_calls"] == 0 and report["predicted"] == 1 and report["failed"] == 0
    curve = json.loads((args.out / "predictions.jsonl").read_text(encoding="utf-8"))
    assert curve["timestamps"][0] == times[0].value // 1_000_000
    assert curve["model_age_seconds"] == 2 * 86400


def test_reject_overlap_wrong_identity_and_corruption(saved):
    directory, _ = saved
    bank = SavedProphetModels(directory)
    with pytest.raises(ValueError, match="重叠"):
        bank.predict("w", "app", "cpu_request", pd.date_range("2026-01-06", periods=24, freq="h"))
    with pytest.raises(ValueError, match="缺少"):
        bank.predict("w", "app", "memory_request", pd.date_range("2026-01-09", periods=24, freq="h"))
    with pytest.raises(ValueError, match="不匹配"):
        SavedProphetModels(directory, "openstack_vm")
    entry = next(iter(bank.entries.values()))
    path = directory / entry["file"]
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        bank.predict("w", "app", "cpu_request", pd.date_range("2026-01-09", periods=24, freq="h"))


def test_expired_model_fails_without_retraining(saved, tmp_path):
    directory, raw = saved
    args = parser().parse_args(["predict", "--models", str(directory), "--raw", str(raw),
                               "--out", str(tmp_path / "old"), "--max-age", "24h"])
    report = predict(args)
    assert report["predicted"] == 0 and report["failed"] == 1 and report["fit_calls"] == 0
    assert "过期" in report["failures"][0]["error"]


def test_lstm_saved_baseline_no_fit_and_leakage_guard(saved, tmp_path, monkeypatch):
    pytest.importorskip("torch")
    from prophet import Prophet
    from training.lstm import parser as lstm_parser, run
    directory, raw = saved
    monkeypatch.setattr(Prophet, "fit", lambda *_args, **_kwargs: pytest.fail("冻结基线不允许 fit"))
    args = lstm_parser().parse_args(["--raw", str(raw), "--out", str(tmp_path / "lstm"),
                                   "--resource-type", "k8s_workload", "--level", "container", "--metric", "cpu_request",
                                   "--epochs", "1", "--hidden-size", "4", "--prophet-models", str(directory)])
    report = run(args)
    assert report["summary"]["prophet_saved"]["successful_windows"] == 1
    assert "prophet" not in report["summary"]
    args.out = tmp_path / "leaky"
    args.train_end = "2026-01-06"
    with pytest.raises(ValueError, match="超过 LSTM 训练截止点"):
        run(args)
