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

    timing_total = float(sum(timing_by_model.values()))
    best_methods: Dict[str, str] = {}
    metrics_out: Dict[str, Dict[str, Dict[str, float]]] = {}
    charts_forecast: Dict[str, Dict[str, Any]] = {}
    observed_stats: Dict[str, Dict[str, float]] = {}
    futures_for_advice: Dict[str, np.ndarray] = {}
    forecast_diagnostics: Dict[str, Any] = {}
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

    advice = None
    # 根据资源类型构建对应的 scaling_advice
    if resource_type == "k8s_workload" and len(futures_for_advice) == len(metric_names):
        advice = build_k8s_workload_advice(futures_for_advice, resource=source)
    elif len(futures_for_advice) == len(METRIC_NAMES):
        advice = build_scaling_advice(
            futures_for_advice,
            current_spec=spec,
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
        "charts_forecast": charts_forecast,
        "forecast_diagnostics": forecast_diagnostics,
        "resource_profile": resource_profile,
        "_timings": {"by_model": timing_by_model, "total": timing_total, "wall": wall_seconds},
        "_slot": i,
    }
    if isinstance(source.get("data_quality"), dict):
        item["data_quality"] = source["data_quality"]
    if advice is not None:
        item["scaling_advice"] = advice
    return item
