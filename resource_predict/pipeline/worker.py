"""单资源预测 worker。"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any, Dict, Iterator, List

import numpy as np
import pandas as pd

from resource_predict.resource_types import METRIC_NAMES, metric_names_for_resource, resource_type_of
from resource_predict.core.decision import build_scaling_advice
from resource_predict.core.k8s_workload_decision import build_k8s_workload_advice
from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.fit import fit_one_metric
from resource_predict.pipeline.resource_profile import build_resource_profile
from resource_predict.pipeline.series_utils import series_to_lists, to_ms
from resource_predict.utils import compute_metric_stats

logger = logging.getLogger(__name__)


def _selected_metrics(source: dict, ctx: WorkerContext) -> list[str]:
    metric_names = metric_names_for_resource(source)
    resource_id = str(source["resource_id"])
    if ctx.metric_partial_enabled and resource_id in ctx.existing_partial_ids:
        selected = [metric for metric in metric_names
                    if metric in ctx.metric_filter_by_id.get(resource_id, set(metric_names))]
        return selected or list(metric_names)
    return list(metric_names)


def _iter_container_inputs(
    source: dict, metric_names: tuple[str, ...], ctx: WorkerContext,
) -> Iterator[tuple[str, str, pd.Series]]:
    raw = source.get("container_metrics")
    if not isinstance(raw, dict):
        return
    for container, metrics in raw.items():
        name = str(container or "").strip()
        if not name or not isinstance(metrics, dict):
            continue
        for metric in metric_names:
            series = metrics.get(metric)
            if series is not None and len(series) > ctx.test_size:
                yield name, metric, series


def iter_metric_inputs(source: dict, ctx: WorkerContext) -> Iterator[tuple[str, str, pd.Series]]:
    """Yield aggregate and eligible container series in the worker's fitting order."""
    for metric in _selected_metrics(source, ctx):
        yield "", metric, _with_identity(source[metric], source, "", metric, ctx)
    for container, metric, series in _iter_container_inputs(source, metric_names_for_resource(source), ctx):
        yield container, metric, _with_identity(series, source, container, metric, ctx)


def _with_identity(series, source, container, metric, ctx):
    from resource_predict.core.accuracy import RATIO_MODES

    result = series.copy(deep=False)
    mode = (source.get("container_metric_modes", {}).get(container, {}).get(metric) if container
            else source.get("spec", {}).get(f"{metric}_metric_mode"))
    result.attrs = {**series.attrs, "forecast_identity": {
        "resource_id": str(source["resource_id"]), "resource_type": resource_type_of(source),
        "container": container, "metric": metric},
        "accuracy_ratio": resource_type_of(source) == "openstack_vm" or mode in RATIO_MODES}
    return result


def worker(
    i: int,
    prepared_data: List[Dict[str, Any]],
    *,
    ctx: WorkerContext,
    parallel_metrics_enabled: bool,
    inner_metric_workers: int,
    precomputed: dict[tuple[str, str], tuple] | None = None,
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

    metric_sources = {}
    for name in metric_names:
        sequence = _with_identity(source[name], source, "", name, ctx)
        metric_sources[name] = (sequence.iloc[:-ctx.test_size], sequence.iloc[-ctx.test_size:], sequence)
    metrics_to_fit = _selected_metrics(source, ctx)

    if precomputed is not None:
        results = [(name, precomputed[("", name)]) for name in metrics_to_fit]
    elif parallel_metrics_enabled and len(metrics_to_fit) > 1:
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
        for m, seconds in timing_part.items():
            timing_by_model[m] = timing_by_model.get(m, 0.0) + float(seconds)

    best_methods: Dict[str, str] = {}
    metrics_out: Dict[str, Dict[str, Dict[str, float]]] = {}
    charts_forecast: Dict[str, Dict[str, Any]] = {}
    observed_stats: Dict[str, Dict[str, float]] = {}
    futures_for_advice: Dict[str, np.ndarray] = {}
    forecast_diagnostics: Dict[str, Any] = {}
    accuracy_holdout: List[Dict[str, Any]] = []
    history_coverage = _history_coverage(source, metric_names)
    for metric_name in metrics_to_fit:
        pred, metric_scores, best, future_pred, _timing, diagnostics = computed[metric_name]
        observed_stats[metric_name] = compute_metric_stats(source[metric_name].to_numpy(dtype=float))
        best_methods[metric_name] = best
        metrics_out[metric_name] = metric_scores
        forecast_diagnostics[metric_name] = diagnostics
        accuracy_holdout.extend(_holdout_curves(source, "", metric_name,
                                               metric_sources[metric_name][1], pred, diagnostics))
        charts_forecast[metric_name] = {
            "x_test_ms": to_ms(metric_sources[metric_name][1].index),
            "preds": {m: series_to_lists(pred[m]) for m in pred.keys()},
            "x_pred_ms": to_ms(next(iter(future_pred.values())).index),
            "preds_future": {m: series_to_lists(future_pred[m]) for m in future_pred.keys()},
            "metrics": metric_scores,
            "best_method": best,
            "test_end_ms": int(metric_sources[metric_name][1].index.max().value // 1_000_000),
            "sample_interval_seconds": float(ctx.sample_interval_seconds),
            "max_interpolation_gap_steps": int(ctx.max_interpolation_gap_steps),
        }
        futures_for_advice[metric_name] = future_pred[best].to_numpy(dtype=float)

    container_charts_forecast, container_futures_for_advice = _fit_container_metrics(
        source,
        metric_names=metric_names,
        ctx=ctx,
        timing_by_model=timing_by_model,
        accuracy_holdout=accuracy_holdout,
        precomputed=precomputed,
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
        "_accuracy_holdout": accuracy_holdout,
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


def _holdout_curves(source, container, metric, y_test, predictions, diagnostics):
    """Freeze prepared outer-test labels; never infer unfilled production telemetry."""
    provenance = {**diagnostics.get("provenance", {}), "actual_source": "prepared_history"}
    evidence = source.get("observation_evidence", {})
    if isinstance(evidence, dict) and evidence.get("source"):
        provenance["input_observation_source"] = evidence["source"]
    failures = diagnostics.get("phase_failures", {})
    methods = dict.fromkeys([*predictions, *failures.get("test", {}), *failures.get("test_fallback", {})])
    actual = [float(value) if np.isfinite(value) else None for value in y_test.to_numpy(dtype=float)]
    curves = []
    for model in methods:
        prediction = predictions.get(model)
        values = prediction.reindex(y_test.index).to_numpy(dtype=float) if prediction is not None else np.full(len(y_test), np.nan)
        curves.append(dict(container=container, metric=metric, model=model, x_test_ms=to_ms(y_test.index),
                           yhat=[float(value) if np.isfinite(value) else None for value in values],
                           actual=actual, evaluation=diagnostics.get("evaluation", {}), provenance=provenance))
    return curves


def _fit_container_metrics(
    source: Dict[str, Any],
    *,
    metric_names: tuple[str, ...],
    ctx: WorkerContext,
    timing_by_model: Dict[str, float],
    accuracy_holdout: List[Dict[str, Any]],
    precomputed: dict[tuple[str, str], tuple] | None = None,
) -> tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Dict[str, np.ndarray]]]:
    charts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    futures: Dict[str, Dict[str, np.ndarray]] = {}
    for name, metric_name, series in _iter_container_inputs(source, metric_names, ctx):
        series = _with_identity(series, source, name, metric_name, ctx)
        y_train = series.iloc[:-ctx.test_size]
        y_test = series.iloc[-ctx.test_size:]
        result = (precomputed[(name, metric_name)] if precomputed is not None
                  else fit_one_metric(y_train, y_test, series, ctx=ctx))
        pred, metric_scores, best, future_pred, timing_part, diagnostics = result
        accuracy_holdout.extend(_holdout_curves(source, name, metric_name, y_test, pred, diagnostics))
        for method, seconds in timing_part.items():
            timing_by_model[method] = timing_by_model.get(method, 0.0) + float(seconds)
        charts.setdefault(name, {})[metric_name] = {
            "x_test_ms": to_ms(y_test.index),
            "preds": {m: series_to_lists(pred[m]) for m in pred.keys()},
            "x_pred_ms": to_ms(next(iter(future_pred.values())).index),
            "preds_future": {m: series_to_lists(future_pred[m]) for m in future_pred.keys()},
            "metrics": metric_scores,
            "best_method": best,
            "forecast_diagnostics": diagnostics,
            "test_end_ms": int(y_test.index.max().value // 1_000_000),
            "sample_interval_seconds": float(ctx.sample_interval_seconds),
            "max_interpolation_gap_steps": int(ctx.max_interpolation_gap_steps),
        }
        futures.setdefault(name, {})[metric_name] = future_pred[best].to_numpy(dtype=float)
    return charts, futures
