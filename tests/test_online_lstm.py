"""已训练 LSTM 参与在线候选选型；真实 torch 推理，无在线梯度更新。"""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from resource_predict.core.lstm_model import SharedLSTM
from resource_predict.core.saved_lstm import forecast_saved_lstm, pin_checkpoint
from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.fit import fit_one_metric
from resource_predict.pipeline.parallel import execute_metric_jobs
from resource_predict.pipeline.plan import resolve_execution_plan
from resource_predict.pipeline.worker import iter_metric_inputs
from resource_predict.services.runtime_config import normalize_runtime_config
from resource_predict.settings import settings


IDENTITY = {"resource_id": "cluster/ns/workload", "resource_type": "k8s_workload", "container": "app", "metric": "cpu_request"}


def checkpoint(path, selection_end="2025-12-02", bias=.8):
    architecture = {"hidden_size": 4, "layers": 1, "dropout": 0.0, "horizon": 4}
    model = SharedLSTM(**architecture)
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.zero_()
        model.head[1].bias.fill_(bias)
    torch.save({"model_version": "shared-lstm-experiment-v3", "architecture": architecture,
                "lookback": 4, "state_dict": model.state_dict(),
                "config": {"resource_type": "k8s_workload", "level": "container"},
                "boundaries": {"train_end_exclusive": "2025-12-01", "validation_end_exclusive": selection_end,
                               "step_seconds": 3600},
                "scalers": [{"resource_id": IDENTITY["resource_id"], "container": "app", "metric": "cpu_request", "mean": 0., "scale": 1.}]}, path)


def context(path, methods=None):
    return WorkerContext(test_size=4, future_steps=2, active_methods=methods or ["rolling_mean", "lstm"],
                         forecast_config={"lstm_model_path": str(path), "lstm_max_age_hours": 0},
                         metric_filter_by_id={}, metric_partial_enabled=False, existing_partial_ids=set(),
                         sample_interval_seconds=3600)


def sequence():
    return pd.Series([.2]*72+[.8]*8, index=pd.date_range("2026-01-01", periods=80, freq="h"))


def test_lstm_wins_validation_and_uses_cached_inference(tmp_path, monkeypatch):
    path = tmp_path / "model.pt"
    checkpoint(path)
    ctx = context(path)
    original_load = torch.load
    loads = []

    def load(*args, **kwargs):
        loads.append(True)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(torch, "load", load)
    monkeypatch.setattr(torch.Tensor, "backward", lambda *_args, **_kwargs: pytest.fail("在线不能训练"))
    full = sequence()
    # 三个验证窗口均为新水平，验证 LSTM 的真实推理和缓存，而非单折默认值。
    full.iloc[-16:] = .8
    result = fit_one_metric(full.iloc[:-4], full.iloc[-4:], full, ctx=ctx, identity=IDENTITY)
    assert result[2] == "lstm" and result[1]["lstm"]["selection_rmse"] < 1e-6
    np.testing.assert_allclose(result[3]["lstm"], .8)
    assert result[5]["saved_lstm"]["eligible_for_selection"]
    assert result[5]["provenance"]["train_end_ms"] < full.index[-1].value // 1_000_000
    assert len(loads) == 1
    # 当前任务继续使用已经固定的旧模型；新一轮采用新的文件 SHA256。
    checkpoint(path, bias=.9)
    old, _ = forecast_saved_lstm(full, pd.date_range(full.index[-1], periods=3, freq="h")[1:], IDENTITY, ctx.lstm_checkpoint, 0)
    new, _ = forecast_saved_lstm(full, old.index, IDENTITY, pin_checkpoint(path), 0)
    np.testing.assert_allclose(old, .8)
    np.testing.assert_allclose(new, .9)


def test_no_selection_from_model_seen_labels(tmp_path):
    path = tmp_path / "model.pt"
    full = sequence()
    checkpoint(path, selection_end=full.index[-4].isoformat())
    result = fit_one_metric(full.iloc[:-4], full.iloc[-4:], full, ctx=context(path), identity=IDENTITY)
    assert result[2] == "rolling_mean"
    assert "lstm" in result[3]  # 可以生成未来曲线，但没有合法选型分数。
    assert not result[5]["saved_lstm"]["eligible_for_selection"]
    assert "重叠" in result[5]["phase_failures"]["validation"]["lstm"]


def test_missing_only_candidate_falls_back_and_identity_checks(tmp_path):
    full = sequence()
    missing = context(tmp_path / "missing.pt", ["lstm"])
    result = fit_one_metric(full.iloc[:-4], full.iloc[-4:], full, ctx=missing, identity=IDENTITY)
    assert result[2] == "rolling_mean" and "lstm" not in result[3]
    path = tmp_path / "model.pt"
    checkpoint(path)
    descriptor = pin_checkpoint(path)
    future = pd.date_range(full.index[-1], periods=3, freq="h")[1:]
    for identity, message in (({**IDENTITY, "container": ""}, "层级"),
                              ({**IDENTITY, "resource_id": "unknown"}, "归一化")):
        with pytest.raises(ValueError, match=message):
            forecast_saved_lstm(full, future, identity, descriptor, 0)
    with pytest.raises(ValueError, match="滞后"):
        forecast_saved_lstm(full, future, IDENTITY, descriptor, 1)
    with pytest.raises(ValueError, match="输出长度"):
        forecast_saved_lstm(full, pd.date_range(future[0], periods=5, freq="h"), IDENTITY, descriptor, 0)


@pytest.mark.parametrize("backend", ["serial", "thread", "process"])
def test_identity_and_model_in_parallel_pipeline(tmp_path, backend):
    path = tmp_path / "model.pt"
    checkpoint(path)
    ctx = context(path)
    full = sequence()
    source = {"resource_id": IDENTITY["resource_id"], "resource_type": "k8s_workload",
              **{name: full for name in ("cpu_request", "cpu_limit", "memory_request", "memory_limit")},
              "container_metrics": {"app": {"cpu_request": full}}}
    jobs = [(0, container, metric, series) for container, metric, series in iter_metric_inputs(source, ctx) if container]
    jobs = jobs * 2
    plan = resolve_execution_plan(len(jobs), ctx.active_methods, backend=backend, max_workers=2)
    results = list(execute_metric_jobs(jobs, ctx, settings.freeze(), plan, {}))
    assert all(result[3][2] == "lstm" for result in results)
    assert "forecast_identity" not in full.attrs


def test_runtime_lstm_fields_validate():
    value = normalize_runtime_config({"prediction": {"enabled_methods": ["lstm", "rolling_mean"],
                                                     "lstm_model_path": "/models/model.pt", "lstm_max_age_hours": 0}})
    assert value.prediction.lstm_model_path == "/models/model.pt"
    for payload in ({"enabled_methods": ["lstm"]}, {"lstm_max_age_hours": -1}):
        with pytest.raises(ValueError):
            normalize_runtime_config({"prediction": payload})


def test_complete_artifacts_contain_lstm(tmp_path):
    from resource_predict.pipeline.run import generate_forecasts
    path = tmp_path / "model.pt"
    checkpoint(path)
    full = sequence()
    data = [{"resource_id": IDENTITY["resource_id"], "resource_type": "k8s_workload", "spec": {"containers": {}},
             "metrics": {name: full for name in ("cpu_request", "cpu_limit", "memory_request", "memory_limit")},
             "container_metrics": {"app": {"cpu_request": full}}}]
    snapshot = settings.freeze()
    frozen = replace(snapshot, forecast=replace(snapshot.forecast, enabled_methods=("rolling_mean", "lstm"),
                                                lstm_model_path=str(path), lstm_max_age_hours=0),
                     k8s_prometheus=replace(snapshot.k8s_prometheus, step_seconds=3600))
    with settings.use(frozen):
        items = generate_forecasts(out_dir=str(tmp_path / "out"), test_size=4, future_steps=2,
                                   data_provider=lambda **_kwargs: data, parallel_backend="serial", freq="h")
    assert "lstm" in items[0]["container_charts_forecast"]["app"]["cpu_request"]["preds_future"]
    errors = json.loads((tmp_path / "out" / "forecast_error_report.json").read_text(encoding="utf-8"))
    assert any(row["model"] == "lstm" and row["container"] == "app" for row in errors["rows"])
