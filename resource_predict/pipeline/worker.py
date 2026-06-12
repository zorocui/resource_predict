"""单资源预测 worker。"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any, Dict, List

import numpy as np

from resource_predict.resource_types import METRIC_NAMES, metric_names_for_resource, resource_type_of
from resource_predict.core.decision import build_scaling_advice
from resource_predict.core.k8s_workload_decision import build_k8s_workload_advice
from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.fit import fit_one_metric
from resource_predict.pipeline.resource_profile import build_resource_profile
from resource_predict.pipeline.series_utils import series_to_lists, to_ms
from resource_predict.utils import compute_metric_stats

logger = logging.getLogger(__name__)


def worker(
    i: int,
    prepared_data: List[Dict[str, Any]],
    *,
    ctx: WorkerContext,
    parallel_metrics_enabled: bool,
    inner_metric_workers: int,
) -> Dict[str, Any]:
    """处理单个资源的全部指标预测。"""
    worker_started = time.perf_counter()
    source = prepared_data[i]
    resource_tag = source["resource_id"]
    spec = source.get("spec", {})
    resource_type = resource_type_of(source)
    metric_names = metric_names_for_resource(source)
    active_methods = ctx.active_methods

    timing_by_model = {m: 0.0 for m in active_methods}

    metric_sources = {
        name: (source[name].iloc[:-ctx.test_size], source[name].iloc[-ctx.test_size:], source[name])
        for name in metric_names
    }
    if ctx.metric_partial_enabled and str(resource_tag) in ctx.existing_partial_ids:
        metrics_to_fit = [
            metric
            for metric in metric_names
            if metric in ctx.metric_filter_by_id.get(str(resource_tag), set(metric_names))
        ]
    else:
        metrics_to_fit = list(metric_names)
    if not metrics_to_fit:
        metrics_to_fit = list(metric_names)

    if parallel_metrics_enabled and len(metrics_to_fit) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=inner_metric_workers) as imx:
            results = list(
                imx.map(
                    lambda name: (name, fit_one_metric(*metric_sources[name], ctx=ctx)),
                    metrics_to_fit,
                )
            )
    else:
        results = [
            (name, fit_one_metric(*metric_sources[name], ctx=ctx)) for name in metrics_to_fit
        ]

    computed: Dict[str, Any] = {}
    for metric_name, result in results:
        computed[metric_name] = result
        timing_part = result[4]
        for m in active_methods:
            timing_by_model[m] += float(timing_part.get(m, 0.0))

    best_methods: Dict[str, str] = {}
    metrics_out: Dict[str, Dict[str, Dict[str, float]]] = {}
    charts_forecast: Dict[str, Dict[str, Any]] = {}
    observed_stats: Dict[str, Dict[str, float]] = {}
    futures_for_advice: Dict[str, np.ndarray] = {}
    forecast_diagnostics: Dict[str, Any] = {}
    history_coverage = _history_coverage(source, metric_names)
    for metric_name in metrics_to_fit:
        pred, metric_scores, best, future_pred, _timing, diagnostics = computed[metric_name]
        observed_stats[metric_name] = compute_metric_stats(source[metric_name].to_numpy(dtype=float))
        best_methods[metric_name] = best
        metrics_out[metric_name] = metric_scores
        forecast_diagnostics[metric_name] = diagnostics
        charts_forecast[metric_name] = {
            "preds": {m: series_to_lists(pred[m]) for m in pred.keys()},
            "x_pred_ms": to_ms(next(iter(future_pred.values())).index),
            "preds_future": {m: series_to_lists(future_pred[m]) for m in future_pred.keys()},
            "metrics": metric_scores,
            "best_method": best,
        }
        futures_for_advice[metric_name] = future_pred[best].to_numpy(dtype=float)

    container_charts_forecast, container_futures_for_advice = _fit_container_metrics(
        source,
        metric_names=metric_names,
        ctx=ctx,
        timing_by_model=timing_by_model,
    )
    timing_total = float(sum(timing_by_model.values()))

    advice = None
    # 根据资源类型构建对应的 scaling_advice
    if resource_type == "k8s_workload" and len(futures_for_advice) == len(metric_names):
        advice = build_k8s_workload_advice(
            futures_for_advice,
            resource={**source, "history_coverage": history_coverage},
            container_future_values=container_futures_for_advice,
        )
    elif len(futures_for_advice) == len(METRIC_NAMES):
        advice = build_scaling_advice(
            futures_for_advice,
            current_spec=spec,
            history_coverage=history_coverage,
        )
    resource_profile = build_resource_profile(
        resource_type=resource_type,
        futures_by_metric=futures_for_advice,
        advice=advice,
    )

    wall_seconds = time.perf_counter() - worker_started
    item = {
        "resource_id": resource_tag,
        "resource_type": resource_type,
        "spec": spec if isinstance(spec, dict) else {},
        "best_methods": best_methods,
        "metrics": metrics_out,
        "observed_stats": observed_stats,
        "history_coverage": history_coverage,
        "charts_forecast": charts_forecast,
        "forecast_diagnostics": forecast_diagnostics,
        "resource_profile": resource_profile,
        "_timings": {"by_model": timing_by_model, "total": timing_total, "wall": wall_seconds},
        "_slot": i,
    }
    if isinstance(source.get("data_quality"), dict):
        item["data_quality"] = source["data_quality"]
    if container_charts_forecast:
        item["container_charts_forecast"] = container_charts_forecast
    if isinstance(source.get("container_data_quality"), dict):
        item["container_data_quality"] = source["container_data_quality"]
    if isinstance(source.get("container_metric_modes"), dict):
        item["container_metric_modes"] = source["container_metric_modes"]
    if advice is not None:
        item["scaling_advice"] = advice
    return item


def _history_coverage(source: Dict[str, Any], metric_names: tuple[str, ...]) -> Dict[str, Any]:
    spans: Dict[str, float] = {}
    for metric_name in metric_names:
        series = source.get(metric_name)
        index = getattr(series, "index", None)
        if index is None or len(index) < 2:
            spans[metric_name] = 0.0
            continue
        try:
            span_hours = float((index.max() - index.min()).total_seconds()) / 3600.0
        except Exception:
            span_hours = 0.0
        spans[metric_name] = max(0.0, span_hours)
    min_span = min(spans.values()) if spans else 0.0
    max_span = max(spans.values()) if spans else 0.0
    threshold_hours = 5 * 24
    return {
        "span_hours": round(min_span, 2),
        "span_days": round(min_span / 24.0, 2),
        "max_span_hours": round(max_span, 2),
        "max_span_days": round(max_span / 24.0, 2),
        "threshold_hours": threshold_hours,
        "threshold_days": 5,
        "is_short": min_span < threshold_hours,
        "metric_spans_hours": {key: round(value, 2) for key, value in spans.items()},
    }


def _fit_container_metrics(
    source: Dict[str, Any],
    *,
    metric_names: tuple[str, ...],
    ctx: WorkerContext,
    timing_by_model: Dict[str, float],
) -> tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Dict[str, np.ndarray]]]:
    raw = source.get("container_metrics")
    if not isinstance(raw, dict):
        return {}, {}
    charts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    futures: Dict[str, Dict[str, np.ndarray]] = {}
    for container, metrics in raw.items():
        name = str(container or "").strip()
        if not name or not isinstance(metrics, dict):
            continue
        for metric_name in metric_names:
            series = metrics.get(metric_name)
            if series is None or len(series) <= ctx.test_size:
                continue
            y_train = series.iloc[:-ctx.test_size]
            y_test = series.iloc[-ctx.test_size:]
            pred, metric_scores, best, future_pred, timing_part, diagnostics = fit_one_metric(y_train, y_test, series, ctx=ctx)
            for method in ctx.active_methods:
                timing_by_model[method] += float(timing_part.get(method, 0.0))
            charts.setdefault(name, {})[metric_name] = {
                "preds": {m: series_to_lists(pred[m]) for m in pred.keys()},
                "x_pred_ms": to_ms(next(iter(future_pred.values())).index),
                "preds_future": {m: series_to_lists(future_pred[m]) for m in future_pred.keys()},
                "metrics": metric_scores,
                "best_method": best,
                "forecast_diagnostics": diagnostics,
            }
            futures.setdefault(name, {})[metric_name] = future_pred[best].to_numpy(dtype=float)
    return charts, futures
