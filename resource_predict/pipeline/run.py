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
from resource_predict.core.forecasting import (
    forecast_arima,
    forecast_prophet,
    forecast_rolling_mean,
    forecast_sarima,
    forecast_seasonal_naive,
)
from resource_predict.core.k8s_workload_decision import build_k8s_workload_advice
from resource_predict.resource_types import METRIC_NAMES, metric_names_for_resource, resource_type_of
from resource_predict.pipeline.constants import MANIFEST_FILENAME, RAW_DATA_FILENAME
from resource_predict.pipeline.partial import load_existing_forecast_items, merge_partial_forecast_items
from resource_predict.pipeline.plan import normalize_metric_filter, resolve_parallel_plan
from resource_predict.pipeline.prepare import ExternalProvider, build_prepared_data
from resource_predict.pipeline.windowing import infer_series_freq, resolve_forecast_window
from resource_predict.pipeline.write_outputs import write_prediction_outputs
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
        if save_raw and data_provider is None:
            write_raw_dataset(raw_path, prepared_data, freq=freq)

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
    for p in prepared_data:
        rid = p["resource_id"]
        metric_names = metric_names_for_resource(p)
        min_len = min(len(p[m]) for m in metric_names)
        if min_len <= test_size:
            raise ValueError(
                f"{rid} 有效点数不足：最短序列长度={min_len}，需大于 test_size={test_size}"
            )
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
    logger.info(
        "[progress] 线程池：max_workers=%d, parallel_metrics=%s, inner_workers=%d, metric_partial=%s",
        max_workers,
        parallel_metrics_enabled,
        inner_metric_workers,
        metric_partial_enabled,
    )

    forecast_config = read_forecast_config()
    active_methods: List[str] = []
    enabled_methods = set(forecast_config["enabled_methods"])
    if "arima" in enabled_methods:
        active_methods.append("arima")
    if "sarima" in enabled_methods:
        active_methods.append("sarima")
    if "prophet" in enabled_methods:
        active_methods.append("prophet")
    if "seasonal_naive" in enabled_methods:
        active_methods.append("seasonal_naive")
    if "rolling_mean" in enabled_methods:
        active_methods.append("rolling_mean")
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

    def _forecast_by_method(method_name: str, y_train: pd.Series, steps: int):
        if method_name == "arima":
            return forecast_arima(y_train, steps)
        if method_name == "sarima":
            return forecast_sarima(y_train, steps)
        if method_name == "prophet":
            return forecast_prophet(y_train, steps)
        if method_name == "seasonal_naive":
            return forecast_seasonal_naive(y_train, steps)
        if method_name == "rolling_mean":
            return forecast_rolling_mean(y_train, steps)
        raise ValueError(f"Unsupported forecast method: {method_name}")

    def _rolling_backtest_metrics(y_full: pd.Series, method_name: str) -> Dict[str, float]:
        folds = max(1, int(getattr(settings.forecast, "rolling_backtest_folds", 1)))
        fold_size = int(test_size)
        min_train = max(fold_size, 24)
        if folds <= 1 or len(y_full) < min_train + fold_size * 2:
            return {}
        scores: List[Dict[str, float]] = []
        max_folds = min(folds, max(1, (len(y_full) - min_train) // fold_size))
        for fold in range(max_folds):
            test_end = len(y_full) - fold * fold_size
            test_start = test_end - fold_size
            if test_start < min_train:
                break
            train = y_full.iloc[:test_start]
            test = y_full.iloc[test_start:test_end]
            if len(train) <= 1 or len(test) != fold_size:
                continue
            try:
                res = _forecast_by_method(method_name, train, fold_size)
                pred = res.yhat.copy()
                pred.index = test.index
                scores.append(_metrics(test, pred))
            except Exception:
                continue
        if not scores:
            return {}
        return {
            "rolling_mae": float(np.mean([s["mae"] for s in scores])),
            "rolling_rmse": float(np.mean([s["rmse"] for s in scores])),
            "rolling_folds": float(len(scores)),
        }

    def _anomaly_profile(y_full: pd.Series) -> Dict[str, Any]:
        values = y_full.to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if values.size < 8:
            return {
                "is_anomalous": False,
                "robust_zscore": 0.0,
                "recent_value": float(values[-1]) if values.size else 0.0,
                "route": "normal",
            }
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        scale = max(1.4826 * mad, 1e-6)
        recent_window = values[-min(3, values.size):]
        recent_value = float(np.max(recent_window))
        robust_z = float(abs(recent_value - median) / scale)
        threshold = float(settings.forecast.anomaly_route_zscore_threshold)
        return {
            "is_anomalous": bool(robust_z >= threshold),
            "robust_zscore": round(robust_z, 3),
            "recent_value": recent_value,
            "median": median,
            "mad": mad,
            "route": "robust" if robust_z >= threshold else "normal",
        }

    def _ensemble_series(
        preds_by_method: Dict[str, pd.Series],
        metrics_by_method: Dict[str, Dict[str, float]],
    ) -> Optional[pd.Series]:
        if not preds_by_method or not bool(forecast_config["enable_ensemble"]):
            return None
        weighted_values = None
        total_weight = 0.0
        index = next(iter(preds_by_method.values())).index
        for method_name, series in preds_by_method.items():
            score = float(metrics_by_method.get(method_name, {}).get("selection_rmse", np.nan))
            if not np.isfinite(score):
                score = float(metrics_by_method.get(method_name, {}).get("rmse", np.nan))
            if not np.isfinite(score):
                continue
            weight = 1.0 / max(score, 1e-6)
            arr = series.to_numpy(dtype=float)
            weighted_values = arr * weight if weighted_values is None else weighted_values + arr * weight
            total_weight += weight
        if weighted_values is None or total_weight <= 0:
            return None
        return pd.Series(weighted_values / total_weight, index=index, name="yhat")

    def _mean_metric(metrics_by_method: Dict[str, Dict[str, float]], name: str) -> Optional[float]:
        values = [
            float(v[name])
            for k, v in metrics_by_method.items()
            if k != "ensemble" and v.get(name) is not None
        ]
        return float(np.mean(values)) if values else None

    def _choose_best_method(
        *,
        metrics_by_method: Dict[str, Dict[str, float]],
        anomaly: Dict[str, Any],
    ) -> str:
        candidates = list(metrics_by_method.keys())
        if not candidates:
            raise ValueError("no forecast candidates")
        best = min(
            candidates,
            key=lambda k: metrics_by_method[k].get("selection_rmse", metrics_by_method[k].get("rmse", float("inf"))),
        )
        if not anomaly.get("is_anomalous"):
            return best
        robust_candidates = [m for m in ("ensemble", "seasonal_naive", "rolling_mean") if m in metrics_by_method]
        if not robust_candidates:
            return best
        return min(
            robust_candidates,
            key=lambda k: metrics_by_method[k].get("selection_rmse", metrics_by_method[k].get("rmse", float("inf"))),
        )

    def _build_resource_profile(
        *,
        resource_type: str,
        futures_by_metric: Dict[str, np.ndarray],
        advice: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        metric_scores: Dict[str, float] = {}
        for metric, values in futures_by_metric.items():
            arr = np.asarray(values, dtype=float)
            arr = arr[np.isfinite(arr)]
            metric_scores[metric] = float(np.percentile(arr, 95)) if arr.size else 0.0
        hot = [m for m, v in metric_scores.items() if v >= 0.8]
        cold = [m for m, v in metric_scores.items() if v <= 0.2]
        dominant = max(metric_scores, key=metric_scores.get) if metric_scores else ""
        if len(hot) >= 2:
            shape = "balanced_pressure"
        elif dominant == "cpu" and hot:
            shape = "compute_bound"
        elif dominant == "memory" and hot:
            shape = "memory_bound"
        elif dominant == "disk" and hot:
            shape = "storage_bound"
        elif len(cold) == len(metric_scores) and metric_scores:
            shape = "idle_candidate"
        else:
            shape = "steady"
        metric_actions = {}
        if isinstance(advice, dict) and isinstance(advice.get("metric_actions"), dict):
            metric_actions = advice["metric_actions"]
        return {
            "resource_type": resource_type,
            "shape": shape,
            "dominant_metric": dominant,
            "hot_metrics": hot,
            "cold_metrics": cold,
            "metric_p95": {k: round(v, 4) for k, v in metric_scores.items()},
            "metric_actions": metric_actions,
        }

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
            anomaly = _anomaly_profile(y_full)
            for m in active_methods:
                res = _forecast_by_method(m, y_train, test_size)
                pred = res.yhat.copy()
                pred.index = y_test.index
                preds[m] = pred
                metrics[m] = _metrics(y_test, pred)
                rolling = _rolling_backtest_metrics(y_full, m)
                metrics[m].update(rolling)
                if rolling.get("rolling_rmse") is not None:
                    metrics[m]["selection_rmse"] = (
                        0.65 * float(metrics[m]["rmse"])
                        + 0.35 * float(rolling["rolling_rmse"])
                    )
                else:
                    metrics[m]["selection_rmse"] = float(metrics[m]["rmse"])
                timing_local[m] += float(res.seconds)

            ensemble_pred = _ensemble_series(preds, metrics)
            if ensemble_pred is not None:
                preds["ensemble"] = ensemble_pred
                metrics["ensemble"] = _metrics(y_test, ensemble_pred)
                rolling_rmse = _mean_metric(metrics, "rolling_rmse")
                if rolling_rmse is not None:
                    metrics["ensemble"]["rolling_rmse"] = rolling_rmse
                    rolling_mae = _mean_metric(metrics, "rolling_mae")
                    if rolling_mae is not None:
                        metrics["ensemble"]["rolling_mae"] = rolling_mae
                    metrics["ensemble"]["rolling_folds"] = max(
                        float(v.get("rolling_folds", 0.0))
                        for k, v in metrics.items()
                        if k != "ensemble"
                    )
                    metrics["ensemble"]["selection_rmse"] = (
                        0.65 * float(metrics["ensemble"]["rmse"])
                        + 0.35 * rolling_rmse
                    )
                else:
                    metrics["ensemble"]["selection_rmse"] = float(metrics["ensemble"]["rmse"])

            best = _choose_best_method(metrics_by_method=metrics, anomaly=anomaly)

            preds_future: Dict[str, pd.Series] = {}
            for m in active_methods:
                res = _forecast_by_method(m, y_full, future_steps)
                preds_future[m] = res.yhat.copy()
                timing_local[m] += float(res.seconds)
            ensemble_future = _ensemble_series(preds_future, metrics)
            if ensemble_future is not None:
                preds_future["ensemble"] = ensemble_future
            diagnostics = {
                "anomaly_profile": anomaly,
                "routing": {
                    "selected_method": best,
                    "route": anomaly.get("route", "normal"),
                    "reason": "recent anomaly routed to robust candidate"
                    if anomaly.get("is_anomalous")
                    else "normal model selection",
                },
            }
            return preds, metrics, best, preds_future, timing_local, diagnostics

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
        forecast_diagnostics: Dict[str, Any] = {}
        for metric_name in metrics_to_fit:
            pred, metric_scores, best, future_pred, _timing, diagnostics = computed[metric_name]
            best_methods[metric_name] = best
            metrics_out[metric_name] = metric_scores
            forecast_diagnostics[metric_name] = diagnostics
            charts_forecast[metric_name] = {
                "preds": {m: _series_to_lists(pred[m]) for m in pred.keys()},
                "x_pred_ms": _to_ms(next(iter(future_pred.values())).index),
                "preds_future": {m: _series_to_lists(future_pred[m]) for m in future_pred.keys()},
                "metrics": metric_scores,
                "best_method": best,
            }
            futures_for_advice[metric_name] = future_pred[best].to_numpy(dtype=float)

        advice = None
        if resource_type in {"k8s_pod", "k8s_workload"} and len(futures_for_advice) == len(metric_names):
            advice = build_k8s_workload_advice(futures_for_advice, resource=source)
        elif len(futures_for_advice) == len(METRIC_NAMES):
            advice = build_scaling_advice(
                futures_for_advice,
                current_spec=spec,
            )
        resource_profile = _build_resource_profile(
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
