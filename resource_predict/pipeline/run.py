from __future__ import annotations

import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from resource_predict.settings import settings
from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from resource_predict.pipeline.action_gate_state import (
    apply_action_gate_confirmations,
    load_action_gate_state,
    write_action_gate_state,
)
from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.constants import MANIFEST_FILENAME
from resource_predict.pipeline.forecast_archive import archive_forecasts
from resource_predict.pipeline.calibration import calibrate_forecasts, refresh_calibration_advice
from resource_predict.pipeline.controlled_activation import apply_controlled_advice
from resource_predict.pipeline.shadow import build_shadow_advice
from resource_predict.pipeline.realized_error import try_score_realized_forecasts
from resource_predict.pipeline.partial import load_existing_forecast_items, merge_partial_forecast_items
from resource_predict.pipeline.plan import normalize_metric_filter, resolve_parallel_plan
from resource_predict.pipeline.prepare import (
    ExternalProvider,
    build_prepared_data,
    prepare_recent_contiguous_forecast_data,
)
from resource_predict.pipeline.windowing import (
    infer_series_freq,
    resolve_forecast_window,
    resource_family_for_items,
)
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
    输出分离：raw_index.json + raw/（观测）、details 分片（预测）、manifest（预测清单）。
    predict_only=True 时从资源级 raw 分片读取，不覆盖原始数据。
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
    partial_resource_ids: Set[str] = {str(x) for x in (resource_ids or []) if str(x)}
    metric_filter_by_id = normalize_metric_filter(metric_names_by_resource)
    existing_items_for_partial: List[Dict[str, Any]] = []
    existing_partial_ids: Set[str] = set()
    metric_partial_enabled = False
    raw_stats: Dict[str, int] = {}

    if predict_only:
        if data_provider is not None:
            raise ValueError("predict_only=True 时不应再传入 data_provider")
        raw_store = RawResourceStore(
            out_base,
            max_cache_items=int(settings.generation.raw_resource_cache_items),
        )
        raw_meta = raw_store.metadata()
        raw_stats = {
            "resources": len(raw_store.resource_ids()),
            "files_total": len(raw_store.resource_ids()),
            "files_written": 0,
            "files_reused": 0,
            "files_removed": 0,
            "index_bytes": int(raw_store.index_path.stat().st_size),
        }
        prepared_data = raw_store.read_many(partial_resource_ids or None)
        if partial_resource_ids:
            if not prepared_data:
                raise ValueError(
                    "resource_ids 没有匹配 raw_index.json 中的任何资源: "
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
        prepared_data = build_prepared_data(
            resources=resources,
            n=n,
            test_size=int(explicit_test_size or 0),
            freq=freq,
            base_seed=base_seed,
            data_provider=data_provider,
            cfg=cfg,
        )
        resources_ct = len(prepared_data)
        # 注：raw 写盘延迟到频率推断完成后（下方统一执行），避免用初始频率写入。
        # data_provider 路径已在 build_prepared_data 内做 checkpoint 写入作为安全网。

    resource_family = resource_family_for_items(prepared_data)
    if resource_family == "workload":
        configured_step = int(settings.k8s_prometheus.step_seconds)
        freq = pd.tseries.frequencies.to_offset(
            pd.Timedelta(seconds=configured_step)
        ).freqstr
    window = resolve_forecast_window(
        cfg=cfg,
        items=prepared_data,
        explicit_test_size=explicit_test_size,
        explicit_future_steps=explicit_future_steps,
        fallback_freq=freq,
        prefer_fallback_freq=resource_family == "workload",
    )
    test_size = window.test_size
    future_steps = window.future_steps
    if resource_family != "workload":
        try:
            first_series = _first_metric_series(prepared_data)
            if first_series is not None:
                freq = infer_series_freq(first_series.index)
        except Exception:
            pass
    if not predict_only and save_raw:
        raw_stats = write_raw_resource_dataset(out_base, prepared_data, freq=freq)
        logger.info(
            "[raw] 资源分片提交完成：resources=%d written=%d reused=%d removed=%d",
            raw_stats["resources"],
            raw_stats["files_written"],
            raw_stats["files_reused"],
            raw_stats["files_removed"],
        )
    # Preserve untrimmed evidence even when a metric cannot currently be forecast.
    realized_items = prepared_data
    prediction_skips: List[Dict[str, str]] = []
    if resource_family == "workload":
        prepared_data, prediction_skips = prepare_recent_contiguous_forecast_data(
            prepared_data,
            sample_interval_seconds=float(window.sample_interval_seconds),
            max_gap_steps=int(settings.k8s_prometheus.max_interpolation_gap_steps),
            test_size=test_size,
        )
    skipped_short: List[str] = []
    for p in prepared_data:
        rid = p["resource_id"]
        metric_names = metric_names_for_resource(p)
        min_len = min(len(p[m]) for m in metric_names)
        if min_len <= test_size:
            skipped_short.append(rid)
    retained_skip_items = _load_retained_skipped_items(
        out_base,
        skipped_resource_ids=set(skipped_short),
        current_items=prepared_data,
    )
    if skipped_short:
        for rid in skipped_short:
            logger.warning(
                "[progress] 跳过最近连续段有效点数不足的资源：%s"
                "（最近连续段长度 ≤ test_size=%d）",
                rid, test_size,
            )
        prepared_data = [
            p for p in prepared_data if p["resource_id"] not in set(skipped_short)
        ]
        resources_ct = len(prepared_data)
        if resources_ct == 0:
            logger.warning(
                "[progress] 所有待预测资源均因有效点数不足被跳过，"
                "本次保留既有预测产物（test_size=%d）",
                test_size,
            )
    if resources_ct > 0:
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
        sample_interval_seconds=window.sample_interval_seconds,
        max_interpolation_gap_steps=int(settings.k8s_prometheus.max_interpolation_gap_steps),
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

    calibration_started = time.perf_counter()
    calibrate_forecasts(out_base, resources_items, retention_days=settings.forecast.archive_retention_days)
    calibration_seconds = time.perf_counter()-calibration_started
    shadow_started = time.perf_counter()
    build_shadow_advice(resources_items)
    shadow_seconds = time.perf_counter()-shadow_started
    try:
        archive_metadata = archive_forecasts(
            out_base,
            resources_items,
            enabled=bool(getattr(settings.forecast, "archive_enabled", True)),
            retention_days=int(getattr(settings.forecast, "archive_retention_days", 7)),
        )
        logger.info("[forecast_archive] %s", archive_metadata)
    except Exception as exc:
        archive_metadata = {"status": "failed", "path": None, "count": 0, "error": str(exc)}
        logger.warning("[forecast_archive] status=failed; forecasts will continue: %s", exc)

    realized_metadata = try_score_realized_forecasts(out_base, realized_items)
    realized_metadata["calibration_seconds"] = calibration_seconds
    realized_metadata["shadow_generation_seconds"] = shadow_seconds
    predicted_count = len(resources_items)
    predicted_resource_ids = {
        str(item.get("resource_id"))
        for item in resources_items
        if item.get("resource_id") is not None
    }
    resources_items = _restore_skipped_container_forecasts(
        out_base,
        resources_items=resources_items,
        prediction_skips=prediction_skips,
    )
    if retained_skip_items:
        resources_items = merge_partial_forecast_items(
            retained_skip_items,
            resources_items,
        )
        logger.info(
            "[progress] 因数据质量跳过后保留 %d 个既有预测资源",
            len(retained_skip_items),
        )
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

    refresh_calibration_advice(resources_items)
    for item in resources_items:
        comparison = item.get("shadow_comparison")
        if (isinstance(comparison, dict) and comparison.get("status") == "paired"
                and comparison.get("source_spec") != item.get("spec", {})):
            item["shadow_comparison"] = {**comparison, "status": "unavailable", "reason": "current_spec_changed"}
    apply_controlled_advice(resources_items, fresh_ids=predicted_resource_ids,
                            report_path=out_base / "forecast_realized_report.json",
                            archive_metadata=archive_metadata,feedback_metadata=realized_metadata)
    refresh_calibration_advice(resources_items)
    action_gate_state = apply_action_gate_confirmations(
        resources_items,
        eligible_resource_ids=predicted_resource_ids,
        prior_state=load_action_gate_state(out_base),
        retention_days=int(settings.decision.action_gate_state_retention_days),
    )
    for item in resources_items:
        if isinstance(item.get("resource_profile"),dict):
            item["resource_profile"]["metric_actions"] = item.get("scaling_advice",{}).get("metric_actions",{})

    total_elapsed = time.perf_counter() - t_start
    manifest_items = write_prediction_outputs(
        out_base=out_base,
        resources_items=resources_items,
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
        raw_stats=raw_stats,
        prediction_skips=prediction_skips,
        forecast_archive=archive_metadata,
        forecast_realized=realized_metadata,
    )
    try:
        write_action_gate_state(out_base, action_gate_state)
    except Exception as exc:
        logger.warning("[action_gate] 预测产物已写出，但确认状态账本提交失败: %s", exc)

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


def _load_retained_skipped_items(
    out_base: Path,
    *,
    skipped_resource_ids: Set[str],
    current_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not skipped_resource_ids:
        return []
    current_by_id = {
        str(item.get("resource_id")): item
        for item in current_items
        if item.get("resource_id") is not None
    }
    retained: List[Dict[str, Any]] = []
    for old in load_existing_forecast_items(out_base):
        rid = str(old.get("resource_id") or "")
        if rid not in skipped_resource_ids:
            continue
        merged = dict(old)
        current = current_by_id.get(rid, {})
        for field in (
            "spec",
            "data_quality",
            "container_data_quality",
            "container_metric_modes",
        ):
            if isinstance(current.get(field), dict):
                merged[field] = current[field]
        retained.append(merged)
    return retained


def _restore_skipped_container_forecasts(
    out_base: Path,
    *,
    resources_items: List[Dict[str, Any]],
    prediction_skips: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    skipped: Dict[str, List[tuple[str, str]]] = {}
    for entry in prediction_skips:
        metric_path = str(entry.get("metric") or "")
        parts = metric_path.split("/", 2)
        if len(parts) != 3 or parts[0] != "container":
            continue
        skipped.setdefault(str(entry.get("resource_id") or ""), []).append(
            (parts[1], parts[2])
        )
    if not skipped:
        return resources_items
    previous_by_id = {
        str(item.get("resource_id") or ""): item
        for item in load_existing_forecast_items(out_base)
    }
    restored: List[Dict[str, Any]] = []
    for source in resources_items:
        rid = str(source.get("resource_id") or "")
        old = previous_by_id.get(rid)
        if not isinstance(old, dict) or rid not in skipped:
            restored.append(source)
            continue
        item = dict(source)
        charts = _copy_nested_mapping(item.get("container_charts_forecast"))
        old_charts = old.get("container_charts_forecast")
        if isinstance(old_charts, dict):
            for container, metric in skipped[rid]:
                old_container = old_charts.get(container)
                if isinstance(old_container, dict) and isinstance(old_container.get(metric), dict):
                    charts.setdefault(container, {})[metric] = old_container[metric]
        if charts:
            item["container_charts_forecast"] = charts
        restored.append(item)
    return restored


def _copy_nested_mapping(value: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): dict(nested)
        for key, nested in value.items()
        if isinstance(nested, dict)
    }


def generate_predictions_only(**kwargs: Any) -> List[Dict[str, Any]]:
    kwargs = {**kwargs, "predict_only": True}
    kwargs.setdefault("save_raw", False)
    return generate_forecasts(**kwargs)


def _first_metric_series(prepared_data: List[Dict[str, Any]]) -> Any:
    for item in prepared_data:
        for metric in metric_names_for_resource(item):
            series = item.get(metric)
            if series is not None:
                return series
    return None


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
    input_series = []
    for item in prepared_data:
        series = _first_metric_series([item])
        if series is not None:
            input_series.append(series)
    input_lens = [len(series) for series in input_series]
    n_min, n_max = min(input_lens), max(input_lens)
    n_avg = sum(input_lens) / max(1, len(input_lens))
    freq_infer = None
    try:
        freq_infer = pd.infer_freq(input_series[0].index)
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
