from __future__ import annotations

import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from resource_predict.settings import settings
from resource_predict.data.io import read_raw_dataset, write_raw_dataset
from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.constants import MANIFEST_FILENAME, RAW_DATA_FILENAME
from resource_predict.pipeline.partial import load_existing_forecast_items, merge_partial_forecast_items
from resource_predict.pipeline.plan import normalize_metric_filter, resolve_parallel_plan
from resource_predict.pipeline.prepare import ExternalProvider, build_prepared_data
from resource_predict.pipeline.windowing import infer_series_freq, resolve_forecast_window
from resource_predict.pipeline.worker import worker as _worker
from resource_predict.pipeline.write_outputs import write_prediction_outputs
from resource_predict.resource_types import metric_names_for_resource
from resource_predict.services.forecast_config import read_forecast_config

logger = logging.getLogger(__name__)


def generate_forecasts(
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
    out_dir = out_dir or settings.app.out_dir
    explicit_test_size = test_size
    explicit_future_steps = future_steps
    timing_enabled = bool(model_timing_mode and model_timing_mode.lower().strip() == "on")
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
        raw_path = out_base / RAW_DATA_FILENAME
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
    else:
        resources = resources if resources is not None else cfg.resources
        n = n if n is not None else cfg.n
        base_seed = base_seed if base_seed is not None else cfg.base_seed
        freq = freq or cfg.freq
        raw_path = out_base / RAW_DATA_FILENAME
        prepared_data = build_prepared_data(
            resources=resources,
            n=n,
            test_size=int(explicit_test_size or 0),
            freq=freq,
            base_seed=base_seed,
            data_provider=data_provider,
            cfg=cfg,
            raw_checkpoint_path=raw_path if (data_provider is not None and save_raw) else None,
        )
        resources_ct = len(prepared_data)
        # 注：raw 写盘延迟到频率推断完成后（下方统一执行），避免用初始频率写入。
        # data_provider 路径已在 build_prepared_data 内做 checkpoint 写入作为安全网。

    window = resolve_forecast_window(
        cfg=cfg,
        items=prepared_data,
        explicit_test_size=explicit_test_size,
        explicit_future_steps=explicit_future_steps,
    )
    test_size = window.test_size
    future_steps = window.future_steps
    try:
        freq = infer_series_freq(prepared_data[0]["cpu"].index)
    except Exception:
        pass
    if not predict_only and save_raw:
        write_raw_dataset(raw_path, prepared_data, freq=freq)
    skipped_short: List[str] = []
    for p in prepared_data:
        rid = p["resource_id"]
        metric_names = metric_names_for_resource(p)
        min_len = min(len(p[m]) for m in metric_names)
        if min_len <= test_size:
            if predict_only:
                skipped_short.append(rid)
            else:
                raise ValueError(
                    f"{rid} 有效点数不足：最短序列长度={min_len}，需大于 test_size={test_size}"
                )
    if skipped_short:
        for rid in skipped_short:
            logger.warning(
                "[progress] 跳过有效点数不足的资源（predict_only 模式）：%s"
                "（序列长度 ≤ test_size=%d）",
                rid, test_size,
            )
        prepared_data = [
            p for p in prepared_data if p["resource_id"] not in set(skipped_short)
        ]
        resources_ct = len(prepared_data)
        if partial_resource_ids:
            partial_resource_ids = {
                rid for rid in partial_resource_ids if rid not in set(skipped_short)
            }
        if resources_ct == 0:
            logger.warning(
                "[progress] 所有待预测资源均因有效点数不足被跳过，"
                "本次不执行预测（test_size=%d）",
                test_size,
            )
            return []
    _log_input_stats(
        prepared_data,
        resources_ct,
        test_size,
        future_steps,
        freq,
        predict_only=predict_only,
        window_source=window.source,
        sample_interval_seconds=window.sample_interval_seconds,
    )

    max_workers, parallel_metrics_enabled, inner_metric_workers = resolve_parallel_plan(
        resources_ct=resources_ct,
        cfg=cfg,
        max_workers=max_workers,
    )

    forecast_config = read_forecast_config()
    active_methods: List[str] = []
    enabled_methods = set(forecast_config["enabled_methods"])
    for method_name in ("arima", "sarima", "prophet", "seasonal_naive", "rolling_mean"):
        if method_name in enabled_methods:
            active_methods.append(method_name)
    if not active_methods:
        raise ValueError("至少需要启用一个预测模型（ARIMA/SARIMA/Prophet）")

    ctx = WorkerContext(
        test_size=test_size,
        future_steps=future_steps,
        active_methods=active_methods,
        forecast_config=forecast_config,
        metric_filter_by_id=metric_filter_by_id,
        metric_partial_enabled=metric_partial_enabled,
        existing_partial_ids=existing_partial_ids,
    )

    logger.info(
        "[progress] 线程池：max_workers=%d, parallel_metrics=%s, inner_workers=%d, metric_partial=%s",
        max_workers,
        parallel_metrics_enabled,
        inner_metric_workers,
        metric_partial_enabled,
    )

    items: List[Optional[Dict[str, Any]]] = [None] * resources_ct
    t_start = time.perf_counter()

    total_timing_by_model = {m: 0.0 for m in active_methods}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(
                _worker, i, prepared_data,
                ctx=ctx,
                parallel_metrics_enabled=parallel_metrics_enabled,
                inner_metric_workers=inner_metric_workers,
            )
            for i in range(resources_ct)
        ]
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
        forecast_window={
            "resource_family": window.resource_family,
            "test_size": window.test_size,
            "future_steps": window.future_steps,
            "test_duration": window.test_duration,
            "future_duration": window.future_duration,
            "sample_interval_seconds": window.sample_interval_seconds,
            "source": window.source,
        },
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
        out_base / MANIFEST_FILENAME,
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
    return generate_forecasts(**kwargs)


def _log_input_stats(
    prepared_data: List[Dict[str, Any]],
    resources_ct: int,
    test_size: int,
    future_steps: int,
    freq: str,
    *,
    predict_only: bool,
    window_source: str,
    sample_interval_seconds: Optional[float],
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
            "test_size=%d, future_steps=%d, freq=%s, sample_interval_seconds=%s, window_source=%s",
            resources_ct,
            n_min,
            n_max,
            n_avg,
            test_size,
            future_steps,
            freq_display,
            sample_interval_seconds,
            window_source,
        )
    else:
        logger.info(
            "[progress] 开始生成：resources=%d, n_input=[%d~%d] (avg=%.1f), "
            "test_size=%d, future_steps=%d, freq=%s, sample_interval_seconds=%s, window_source=%s",
            resources_ct,
            n_min,
            n_max,
            n_avg,
            test_size,
            future_steps,
            freq_display,
            sample_interval_seconds,
            window_source,
        )
