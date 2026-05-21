from __future__ import annotations

import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np
import pandas as pd

from resource_predict.settings import settings
from resource_predict.data.io import read_raw_dataset, write_raw_dataset
from resource_predict.core.decision import build_scaling_advice
from resource_predict.core.forecasting import forecast_arima, forecast_prophet, forecast_sarima
from resource_predict.core.k8s_pod_decision import build_k8s_pod_advice
from resource_predict.resource_types import METRIC_NAMES, metric_names_for_resource, resource_type_of
from resource_predict.pipeline.partial import load_existing_forecast_items, merge_partial_forecast_items
from resource_predict.pipeline.plan import normalize_metric_filter, resolve_parallel_plan
from resource_predict.pipeline.prepare import ExternalProvider, build_prepared_data
from resource_predict.pipeline.write_outputs import write_prediction_outputs

logger = logging.getLogger(__name__)


def generate_all_images(
    *,
    out_dir: Optional[str] = None,
    resources: Optional[int] = None,
    n: Optional[int] = None,
    test_size: Optional[int] = None,
    future_steps: Optional[int] = None,
    base_seed: Optional[int] = None,
    max_workers: Optional[int] = None,
    data_provider: Optional[ExternalProvider] = None,
    freq: Optional[str] = None,
    model_timing_mode: Optional[str] = None,
    predict_only: bool = False,
    save_raw: Optional[bool] = None,
    resource_ids: Optional[List[str]] = None,
    metric_names_by_resource: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    生成云资源预测结果（并行）。
    输出分离：raw_data.json（观测）、details 分片（预测）、manifest（合并 charts）。
    predict_only=True 时从 raw_data.json 读取，不覆盖原始数据。
    """
    cfg = settings.generation
    out_dir = out_dir or cfg.out_dir
    test_size = test_size if test_size is not None else cfg.test_size
    future_steps = future_steps if future_steps is not None else cfg.future_steps
    timing_mode = (model_timing_mode or cfg.timing_stats_mode).lower().strip()
    if timing_mode not in {"on", "off", "auto"}:
        raise ValueError("model_timing_mode 仅支持: on / off / auto")
    if future_steps <= 0:
        raise ValueError("future_steps 必须为正整数")
    if cfg.detail_chunk_size <= 0:
        raise ValueError("detail_chunk_size 必须为正整数")

    out_base = Path(out_dir)
    out_base.mkdir(parents=True, exist_ok=True)

    if save_raw is None:
        save_raw = bool(cfg.save_raw_dataset)

    prepared_data: List[Dict[str, Any]]
    raw_prepared_data: Optional[List[Dict[str, Any]]] = None
    partial_resource_ids: Set[str] = {str(x) for x in (resource_ids or []) if str(x)}
    metric_filter_by_id = normalize_metric_filter(metric_names_by_resource)
    existing_items_for_partial: List[Dict[str, Any]] = []
    existing_partial_ids: Set[str] = set()
    metric_partial_enabled = False

    if predict_only:
        if data_provider is not None:
            raise ValueError("predict_only=True 时不应再传入 data_provider")
        raw_path = out_base / settings.app.raw_data_filename
        prepared_data, raw_meta = read_raw_dataset(raw_path)
        raw_prepared_data = prepared_data
        if partial_resource_ids:
            prepared_data = [
                p for p in prepared_data if str(p.get("resource_id")) in partial_resource_ids
            ]
            if not prepared_data:
                raise ValueError(
                    "resource_ids 没有匹配 raw_data.json 中的任何资源: "
                    + ", ".join(sorted(partial_resource_ids))
                )
        freq = freq or str(raw_meta.get("freq") or cfg.freq)
        resources_ct = len(prepared_data)
        if partial_resource_ids:
            existing_items_for_partial = load_existing_forecast_items(out_base)
            existing_partial_ids = {
                str(x.get("resource_id"))
                for x in existing_items_for_partial
                if isinstance(x, dict) and x.get("resource_id") is not None
            }
            metric_partial_enabled = bool(existing_items_for_partial and metric_filter_by_id)
        for p in prepared_data:
            rid = p["resource_id"]
            metric_names = metric_names_for_resource(p)
            min_len = min(len(p[m]) for m in metric_names)
            if min_len <= test_size:
                raise ValueError(
                    f"{rid} 有效点数不足：最短序列长度={min_len}，需大于 test_size={test_size}"
                )
        _log_input_stats(prepared_data, resources_ct, test_size, future_steps, freq, predict_only=True)
    else:
        resources = resources if resources is not None else cfg.resources
        n = n if n is not None else cfg.n
        base_seed = base_seed if base_seed is not None else cfg.base_seed
        freq = freq or cfg.freq
        raw_path = out_base / settings.app.raw_data_filename
        prepared_data = build_prepared_data(
            resources=resources,
            n=n,
            test_size=test_size,
            freq=freq,
            base_seed=base_seed,
            data_provider=data_provider,
            cfg=cfg,
            raw_checkpoint_path=raw_path if (data_provider is not None and save_raw) else None,
        )
        resources_ct = len(prepared_data)
        if save_raw and data_provider is None:
            write_raw_dataset(raw_path, prepared_data, freq=freq)
        _log_input_stats(prepared_data, resources_ct, test_size, future_steps, freq, predict_only=False)

    max_workers, parallel_metrics_enabled, inner_metric_workers = resolve_parallel_plan(
        resources_ct=resources_ct,
        cfg=cfg,
        max_workers=max_workers,
    )
    logger.info(
        "[progress] 线程池：max_workers=%d, parallel_metrics=%s, inner_workers=%d, metric_partial=%s",
        max_workers,
        parallel_metrics_enabled,
        inner_metric_workers,
        metric_partial_enabled,
    )

    if timing_mode == "auto":
        timing_enabled = resources_ct <= cfg.timing_stats_auto_resources_threshold
    else:
        timing_enabled = timing_mode == "on"

    active_methods: List[str] = []
    if settings.forecast.enable_arima:
        active_methods.append("arima")
    if settings.forecast.enable_sarima:
        active_methods.append("sarima")
    if settings.forecast.enable_prophet:
        active_methods.append("prophet")
    if not active_methods:
        raise ValueError("至少需要启用一个预测模型（ARIMA/SARIMA/Prophet）")

    items: List[Optional[Dict[str, Any]]] = [None] * resources_ct
    t_start = time.perf_counter()

    def _metrics(y_true: pd.Series, y_pred: pd.Series) -> Dict[str, float]:
        yt = y_true.to_numpy(dtype=float)
        yp = y_pred.to_numpy(dtype=float)
        mae = float(np.mean(np.abs(yt - yp)))
        rmse = float(np.sqrt(np.mean((yt - yp) ** 2)))
        return {"mae": mae, "rmse": rmse}

    def _to_ms(index: pd.DatetimeIndex) -> List[int]:
        return (index.view("int64") // 1_000_000).tolist()

    def _series_to_lists(s: pd.Series) -> List[float]:
        return s.to_numpy(dtype=float).tolist()

    def _worker(i: int) -> Dict[str, Any]:
        worker_started = time.perf_counter()
        source = prepared_data[i]
        resource_tag = source["resource_id"]
        spec = source.get("spec", {})
        resource_type = resource_type_of(source)
        metric_names = metric_names_for_resource(source)

        timing_by_model = {m: 0.0 for m in active_methods}

        def _fit_one_metric(y_train: pd.Series, y_test: pd.Series, y_full: pd.Series):
            timing_local = {m: 0.0 for m in active_methods}
            preds: Dict[str, pd.Series] = {}
            metrics: Dict[str, Dict[str, float]] = {}
            for m in active_methods:
                if m == "arima":
                    res = forecast_arima(y_train, test_size)
                elif m == "sarima":
                    res = forecast_sarima(y_train, test_size)
                else:
                    res = forecast_prophet(y_train, test_size)
                pred = res.yhat.copy()
                pred.index = y_test.index
                preds[m] = pred
                metrics[m] = _metrics(y_test, pred)
                timing_local[m] += float(res.seconds)

            best = min(metrics.keys(), key=lambda k: metrics[k]["rmse"])

            preds_future: Dict[str, pd.Series] = {}
            for m in active_methods:
                if m == "arima":
                    res = forecast_arima(y_full, future_steps)
                elif m == "sarima":
                    res = forecast_sarima(y_full, future_steps)
                else:
                    res = forecast_prophet(y_full, future_steps)
                preds_future[m] = res.yhat.copy()
                timing_local[m] += float(res.seconds)
            return preds, metrics, best, preds_future, timing_local

        metric_sources = {
            name: (source[name].iloc[:-test_size], source[name].iloc[-test_size:], source[name])
            for name in metric_names
        }
        if metric_partial_enabled and str(resource_tag) in existing_partial_ids:
            metrics_to_fit = [
                metric
                for metric in metric_names
                if metric in metric_filter_by_id.get(str(resource_tag), set(metric_names))
            ]
        else:
            metrics_to_fit = list(metric_names)
        if not metrics_to_fit:
            metrics_to_fit = list(metric_names)

        if parallel_metrics_enabled and len(metrics_to_fit) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=inner_metric_workers) as imx:
                results = list(
                    imx.map(
                        lambda name: (name, _fit_one_metric(*metric_sources[name])),
                        metrics_to_fit,
                    )
                )
        else:
            results = [
                (name, _fit_one_metric(*metric_sources[name])) for name in metrics_to_fit
            ]

        computed: Dict[str, Any] = {}
        for metric_name, result in results:
            computed[metric_name] = result
            timing_part = result[4]
            for m in active_methods:
                timing_by_model[m] += float(timing_part.get(m, 0.0))

        timing_total = float(sum(timing_by_model.values()))
        best_methods: Dict[str, str] = {}
        metrics_out: Dict[str, Dict[str, Dict[str, float]]] = {}
        charts_forecast: Dict[str, Dict[str, Any]] = {}
        futures_for_advice: Dict[str, np.ndarray] = {}
        for metric_name in metrics_to_fit:
            pred, metric_scores, best, future_pred, _timing = computed[metric_name]
            best_methods[metric_name] = best
            metrics_out[metric_name] = metric_scores
            charts_forecast[metric_name] = {
                "preds": {m: _series_to_lists(pred[m]) for m in active_methods},
                "x_pred_ms": _to_ms(next(iter(future_pred.values())).index),
                "preds_future": {m: _series_to_lists(future_pred[m]) for m in active_methods},
                "metrics": metric_scores,
                "best_method": best,
            }
            futures_for_advice[metric_name] = future_pred[best].to_numpy(dtype=float)

        advice = None
        if resource_type == "k8s_pod" and len(futures_for_advice) == len(metric_names):
            advice = build_k8s_pod_advice(futures_for_advice, resource=source)
        elif len(futures_for_advice) == len(METRIC_NAMES):
            advice = build_scaling_advice(
                futures_for_advice,
                current_spec=spec,
            )

        wall_seconds = time.perf_counter() - worker_started
        item = {
            "resource_id": resource_tag,
            "resource_type": resource_type,
            "spec": spec if isinstance(spec, dict) else {},
            "best_methods": best_methods,
            "metrics": metrics_out,
            "charts_forecast": charts_forecast,
            "_timings": {"by_model": timing_by_model, "total": timing_total, "wall": wall_seconds},
            "_slot": i,
        }
        if isinstance(source.get("data_quality"), dict):
            item["data_quality"] = source["data_quality"]
        if advice is not None:
            item["scaling_advice"] = advice
        return item

    total_timing_by_model = {m: 0.0 for m in active_methods}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_worker, i) for i in range(resources_ct)]
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            done_count += 1
            idx = int(res.pop("_slot"))
            items[idx] = res
            elapsed = time.perf_counter() - t_start
            t = res.get("_timings", {})
            wall_seconds = float(t.get("wall", 0.0))
            if timing_enabled:
                by_model = t.get("by_model", {})
                for m in active_methods:
                    total_timing_by_model[m] += float(by_model.get(m, 0.0))
            logger.info(
                "[progress] 已完成 %d/%d -> %s (单次 %.1fs | 总 %.1fs)",
                done_count,
                resources_ct,
                res["resource_id"],
                wall_seconds,
                elapsed,
            )

    resources_items: List[Dict[str, Any]] = [x for x in items if x is not None]
    for item in resources_items:
        item.pop("_timings", None)
        item.pop("_slot", None)

    predicted_count = len(resources_items)
    if predict_only and partial_resource_ids:
        existing_items = existing_items_for_partial or load_existing_forecast_items(out_base)
        if existing_items:
            resources_items = merge_partial_forecast_items(
                existing_items,
                resources_items,
                metric_names_by_resource=metric_filter_by_id if metric_partial_enabled else None,
            )
            logger.info(
                "[progress] 增量预测合并完成：本次重算 %d 个资源，输出保留 %d 个资源",
                predicted_count,
                len(resources_items),
            )
        else:
            logger.warning("[progress] 未找到既有预测产物，本次仅输出已重算资源")

    total_elapsed = time.perf_counter() - t_start
    manifest_items = write_prediction_outputs(
        out_base=out_base,
        resources_items=resources_items,
        prepared_data=prepared_data,
        raw_prepared_data=raw_prepared_data,
        active_methods=active_methods,
        test_size=test_size,
        future_steps=future_steps,
        detail_chunk_size=int(cfg.detail_chunk_size),
        predicted_count=predicted_count,
        partial_resource_ids=partial_resource_ids,
        metric_filter_by_id=metric_filter_by_id,
        metric_partial_enabled=metric_partial_enabled,
        total_elapsed=total_elapsed,
    )

    logger.info(
        "[progress] 全部完成：%d/%d，总耗时 %.1fs，输出: %s",
        len(resources_items),
        resources_ct,
        total_elapsed,
        out_base / "manifest.json",
    )
    if timing_enabled and resources_ct > 0:
        total_parts = ", ".join(f"{m}={total_timing_by_model[m]:.2f}s" for m in active_methods)
        avg_parts = ", ".join(
            f"{m}={total_timing_by_model[m] / resources_ct:.2f}s" for m in active_methods
        )
        logger.info(
            "[timing] 模型耗时汇总：total(%s) | avg_per_resource(%s)",
            total_parts,
            avg_parts,
        )
    return manifest_items


def generate_predictions_only(**kwargs: Any) -> List[Dict[str, Any]]:
    kwargs = {**kwargs, "predict_only": True}
    kwargs.setdefault("save_raw", False)
    return generate_all_images(**kwargs)


def _log_input_stats(
    prepared_data: List[Dict[str, Any]],
    resources_ct: int,
    test_size: int,
    future_steps: int,
    freq: str,
    *,
    predict_only: bool,
) -> None:
    cpu_lens = [len(p["cpu"]) for p in prepared_data]
    n_min, n_max = min(cpu_lens), max(cpu_lens)
    n_avg = sum(cpu_lens) / max(1, len(cpu_lens))
    freq_infer = None
    try:
        freq_infer = pd.infer_freq(prepared_data[0]["cpu"].index)
    except Exception:
        freq_infer = None
    freq_display = freq_infer or freq
    if predict_only:
        logger.info(
            "[progress] 仅预测模式：resources=%d, n_input=[%d~%d] (avg=%.1f), "
            "test_size=%d, future_steps=%d, freq=%s",
            resources_ct,
            n_min,
            n_max,
            n_avg,
            test_size,
            future_steps,
            freq_display,
        )
    else:
        logger.info(
            "[progress] 开始生成：resources=%d, n_input=[%d~%d] (avg=%.1f), "
            "test_size=%d, future_steps=%d, freq=%s",
            resources_ct,
            n_min,
            n_max,
            n_avg,
            test_size,
            future_steps,
            freq_display,
        )

