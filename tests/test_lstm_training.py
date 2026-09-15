"""独立实验的时间泄漏边界和 raw 只读契约。"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from resource_predict.data.raw_store import write_raw_resource_dataset
from training.data import SeriesData, load_series, split_windows


def samples():
    times = pd.date_range("2026-01-01", periods=24 * 8, freq="h")
    return [SeriesData(str(i), "", pd.Series(np.arange(len(times), dtype=float) + i, index=times))
            for i in range(2)]


def test_shared_cutoff_scaler_and_windows_ignore_future():
    original = samples()
    changed = samples()
    for item in changed:
        item.series.iloc[-48:] += 10000
    first = split_windows(original, 24, 24, 6, "24h", "24h")
    second = split_windows(changed, 24, 24, 6, "24h", "24h")
    assert [(x.mean, x.scale) for x in first[0]] == [(x.mean, x.scale) for x in second[0]]
    assert first[1] == second[1]
    boundary = pd.Timestamp(first[3]["train_end_exclusive"])
    valid_end = pd.Timestamp(first[3]["validation_end_exclusive"])
    for phase, positions in first[1].items():
        for index, origin in positions:
            times = first[0][index].series.index[origin:origin+24]
            if phase == "train":
                assert times[-1] < boundary
            elif phase == "validation":
                assert boundary <= times[0] and times[-1] < valid_end
            else:
                assert times[0] >= valid_end
    # 日期不同的资源也不能各自按比例切分。
    shifted = samples()
    shifted[0].series = shifted[0].series.iloc[:-12]
    result = split_windows(shifted, 24, 12, 6, "24h", "24h")
    assert result[3]["train_end_exclusive"] == first[3]["train_end_exclusive"]


def test_raw_container_loading_is_read_only(tmp_path):
    series = samples()[0].series
    metrics = {m: series for m in ("cpu_limit", "cpu_request", "memory_limit", "memory_request")}
    item = {"resource_id": "workload-a", "resource_type": "k8s_workload", "spec": {"containers": {}},
            **metrics, "container_metrics": {"app": metrics}}
    write_raw_resource_dataset(tmp_path, [item], freq="h")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*.json")}
    loaded, skipped, source = load_series(tmp_path, "k8s_workload", "cpu_request", "container", 0, 42)
    assert len(loaded) == 1 and loaded[0].container == "app" and not skipped
    assert len(source["sha256"]) == 64
    unified, _, fingerprint = load_series(tmp_path, "k8s_workload", "all", "container", 0, 42)
    assert {s.metric for s in unified} == set(metrics)
    assert len(unified) == 4
    assert fingerprint["sha256"] != source["sha256"]
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_short_history_rejected():
    with pytest.raises(ValueError, match="历史不足"):
        split_windows(samples(), 200, 24, 6, "24h", "24h")


def test_raw_gap_is_not_interpolated(tmp_path):
    series = samples()[0].series.drop(samples()[0].series.index[50])
    item = {"resource_id": "vm-gap", "resource_type": "openstack_vm", "spec": {},
            "cpu": series, "memory": series, "disk": series}
    write_raw_resource_dataset(tmp_path, [item], freq="h")
    with pytest.raises(ValueError, match="没有可用"):
        load_series(tmp_path, "openstack_vm", "cpu", "resource", 0, 42)


@pytest.mark.parametrize("metric", ["cpu", "all"])
def test_training_checkpoint_and_report(tmp_path, metric):
    torch = pytest.importorskip("torch")
    from training.lstm import SharedLSTM, parser, run

    raw = tmp_path / "raw-input"
    series = samples()[0].series / 200
    item = {"resource_id": "vm-a", "resource_type": "openstack_vm", "spec": {},
            "cpu": series, "memory": series * 2, "disk": series * 3}
    write_raw_resource_dataset(raw, [item], freq="h")
    args = parser().parse_args(["--raw", str(raw), "--out", str(tmp_path / "result"),
                               "--resource-type", "openstack_vm", "--metric", metric,
                               "--epochs", "1", "--hidden-size", "4", "--baselines", "seasonal_naive", "rolling_mean"])
    report = run(args)
    assert report["best_epoch"] == 1
    assert set(report["summary"]) == {"lstm", "seasonal_naive", "rolling_mean"}
    checkpoint = torch.load(args.out / "model.pt", weights_only=True)
    expected = {"cpu"} if metric == "cpu" else {"cpu", "memory", "disk"}
    assert set(report["summary_by_metric"]) == expected
    assert report["series_by_metric"] == {name: 1 for name in expected}
    assert {s["metric"] for s in checkpoint["scalers"]} == expected
    assert len(checkpoint["scalers"]) == len(expected)
    assert len({s["mean"] for s in checkpoint["scalers"]}) == len(expected)
    records = json.loads((args.out / "forecast_error_report.json").read_text())["records"]
    assert {r["metric"] for r in records} == expected
    assert all(report["summary_by_metric"][m]["lstm"]["successful_windows"] == 1 for m in expected)
    model = SharedLSTM(**checkpoint["architecture"])
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    with torch.inference_mode():
        assert model(torch.zeros(2, checkpoint["lookback"], 1)).shape == (2, 24)
    assert (Path(args.out) / "forecast_error_report.json").exists()
    with pytest.raises(ValueError, match="输出目录已存在"):
        run(args)


def test_default_is_shared_all_metrics():
    pytest.importorskip("torch")
    from training.lstm import parser
    args = parser().parse_args(["--raw", "input", "--out", "output", "--resource-type", "k8s_workload"])
    assert args.metric == "all"
