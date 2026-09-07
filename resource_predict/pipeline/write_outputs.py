from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np

from resource_predict.data.io import atomic_write_json
from resource_predict.pipeline.constants import (
    DETAILS_DIRNAME,
    FORECAST_ERROR_REPORT_FILENAME,
    GENERATION_STATS_FILENAME,
    MANIFEST_FILENAME,
    RAW_INDEX_FILENAME,
    SUMMARY_INDEX_FILENAME,
)
from resource_predict.resource_types import metric_names_for_resource, resource_type_of


def write_prediction_outputs(
    *,
    out_base: Path,
    resources_items: List[Dict[str, Any]],
    active_methods: List[str],
    test_size: int,
    future_steps: int,
    forecast_window: Dict[str, Any],
    detail_chunk_size: int,
    predicted_count: int,
    partial_resource_ids: Set[str],
    metric_filter_by_id: Dict[str, Set[str]],
    metric_partial_enabled: bool,
    total_elapsed: float,
    raw_stats: Dict[str, int],
    prediction_skips: Optional[List[Dict[str, str]]] = None,
    forecast_archive: Optional[Dict[str, Any]] = None,
    forecast_realized: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    prediction_skips = list(prediction_skips or [])
    details_dir = out_base / DETAILS_DIRNAME
    details_dir.mkdir(parents=True, exist_ok=True)
    details_files: List[str] = []
    summary_resources: List[Dict[str, Any]] = []
    details_lookup: Dict[str, Dict[str, int | str]] = {}

    for chunk_id, start in enumerate(range(0, len(resources_items), detail_chunk_size)):
        chunk_items = resources_items[start : start + detail_chunk_size]
        file_name = f"part-{chunk_id:05d}.json"
        file_path = details_dir / file_name
        details_files.append(file_name)
        atomic_write_json(
            file_path,
            {"resources": chunk_items},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for offset, item in enumerate(chunk_items):
            rid = str(item.get("resource_id"))
            details_lookup[rid] = {"chunk_id": chunk_id, "offset": offset, "file": file_name}

    for item in resources_items:
        rid = str(item.get("resource_id"))
        metric_vals: List[float] = []
        metrics_by_kind = item.get("metrics", {})
        if isinstance(metrics_by_kind, dict):
            for kind in metric_names_for_resource(item):
                kind_metrics = metrics_by_kind.get(kind, {})
                if isinstance(kind_metrics, dict):
                    for method_name in active_methods:
                        metric_obj = kind_metrics.get(method_name, {})
                        if isinstance(metric_obj, dict) and "rmse" in metric_obj:
                            metric_vals.append(float(metric_obj["rmse"]))
        anomaly_score = float(np.mean(metric_vals)) if metric_vals else float("inf")
        row = {
                "resource_id": rid,
                "resource_type": resource_type_of(item),
                "spec": item.get("spec", {}),
                "best_methods": item.get("best_methods", {}),
                "anomaly_score": anomaly_score,
                "scaling_advice": item.get("scaling_advice", {}),
                "observed_stats": item.get("observed_stats", {}),
                "history_coverage": item.get("history_coverage", {}),
                "resource_profile": item.get("resource_profile", {}),
                "detail_ref": details_lookup.get(rid, {}),
        }
        if isinstance(item.get("data_quality"), dict):
            row["data_quality"] = item["data_quality"]
        if isinstance(item.get("shadow_comparison"), dict):
            row["shadow_comparison"] = item["shadow_comparison"]
        summary_resources.append(row)

    summary_resources.sort(
        key=lambda x: (
            -float(x.get("anomaly_score", 0.0)),
            str(x.get("resource_id", "")),
        )
    )

    summary_payload = {
        "meta": {
            "generated_at_epoch_ms": int(time.time() * 1000),
            "resources": len(resources_items),
            "active_methods": active_methods,
            "test_size": test_size,
            "future_steps": future_steps,
            "forecast_window": forecast_window,
            "detail_chunk_size": detail_chunk_size,
            "details_dir": DETAILS_DIRNAME,
            "details_files": details_files,
            "raw_index_file": RAW_INDEX_FILENAME,
            "prediction_skips": prediction_skips,
        },
        "resources": summary_resources,
    }
    atomic_write_json(
        out_base / SUMMARY_INDEX_FILENAME,
        summary_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    manifest_items = [dict(item) for item in resources_items]
    atomic_write_json(
        out_base / MANIFEST_FILENAME,
        {"meta": {"prediction_skips": prediction_skips}, "resources": manifest_items},
        ensure_ascii=False,
        indent=2,
    )
    error_report = _build_forecast_error_report(
        resources_items=resources_items,
        active_methods=active_methods,
        test_size=test_size,
        future_steps=future_steps,
        forecast_window=forecast_window,
        prediction_skips=prediction_skips,
    )
    atomic_write_json(
        out_base / FORECAST_ERROR_REPORT_FILENAME,
        error_report,
        ensure_ascii=False,
        indent=2,
    )

    total_bytes = 0
    for p in [
        out_base / SUMMARY_INDEX_FILENAME,
        out_base / MANIFEST_FILENAME,
        out_base / FORECAST_ERROR_REPORT_FILENAME,
    ]:
        if p.exists():
            total_bytes += int(p.stat().st_size)
    for fn in details_files:
        fp = details_dir / fn
        if fp.exists():
            total_bytes += int(fp.stat().st_size)

    stats_payload = {
        "resources": len(resources_items),
        "predicted_resources": predicted_count,
        "partial_resource_ids": sorted(partial_resource_ids),
        "partial_metrics_by_resource": {
            rid: sorted(names) for rid, names in sorted(metric_filter_by_id.items())
        },
        "metric_partial_enabled": metric_partial_enabled,
        "active_methods": active_methods,
        "test_size": test_size,
        "future_steps": future_steps,
        "forecast_window": forecast_window,
        "detail_files": len(details_files),
        "detail_chunk_size": detail_chunk_size,
        "total_elapsed_seconds": total_elapsed,
        "total_output_bytes": total_bytes,
        "forecast_error_report_file": FORECAST_ERROR_REPORT_FILENAME,
        "forecast_archive": dict(forecast_archive or {}),
        "forecast_realized": dict(forecast_realized or {}),
        "raw": dict(raw_stats),
        "prediction_skips": prediction_skips,
    }
    atomic_write_json(
        out_base / GENERATION_STATS_FILENAME,
        stats_payload,
        ensure_ascii=False,
        indent=2,
    )
    return manifest_items


def _build_forecast_error_report(
    *,
    resources_items: List[Dict[str, Any]],
    active_methods: List[str],
    test_size: int,
    future_steps: int,
    forecast_window: Dict[str, Any],
    prediction_skips: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """构建按资源/指标/模型/窗口展开的预测误差报告。"""
    rows: List[Dict[str, Any]] = []
    resources: List[Dict[str, Any]] = []
    window_info = {
        "test_size": test_size,
        "future_steps": future_steps,
        "resource_family": forecast_window.get("resource_family"),
        "test_duration": forecast_window.get("test_duration"),
        "future_duration": forecast_window.get("future_duration"),
        "sample_interval_seconds": forecast_window.get("sample_interval_seconds"),
        "source": forecast_window.get("source"),
    }
    for item in resources_items:
        rid = str(item.get("resource_id"))
        rtype = resource_type_of(item)
        resource = {"resource_id": rid, "resource_type": rtype,
                    "metrics": {}, "container_metrics": {},
                    "evaluation": {}, "container_evaluation": {}}
        diagnostics_by_metric = item.get("forecast_diagnostics", {})
        groups = [(None, item.get("metrics", {}), diagnostics_by_metric)]
        for container, charts in item.get("container_charts_forecast", {}).items():
            groups.append((container,
                           {metric: chart.get("metrics", {}) for metric, chart in charts.items()},
                           {metric: chart.get("forecast_diagnostics", {}) for metric, chart in charts.items()}))
        for container, metric_group, diagnostics_group in groups:
            if not isinstance(metric_group, dict):
                continue
            for metric in dict.fromkeys([*metric_group, *diagnostics_group]):
                kind_metrics = metric_group.get(metric, {})
                if not isinstance(kind_metrics, dict):
                    continue
                diagnostics = diagnostics_group.get(metric, {})
                model_metrics = _error_model_metrics(kind_metrics, diagnostics, active_methods, window_info)
                if not model_metrics:
                    continue
                evaluation = {key: value for key, value in diagnostics.get("evaluation", {}).items()
                              if key != "validation_metrics"}
                if container is None:
                    resource["metrics"][metric] = model_metrics
                    resource["evaluation"][metric] = evaluation
                else:
                    resource["container_metrics"].setdefault(container, {})[metric] = model_metrics
                    resource["container_evaluation"].setdefault(container, {})[metric] = evaluation
                for method, errors in model_metrics.items():
                    rows.append({"resource_id": rid, "resource_type": rtype,
                                 "container": container, "metric": metric, "model": method, **errors})
        if resource["metrics"] or resource["container_metrics"]:
            resources.append(resource)
    return {
        "meta": {
            "generated_at_epoch_ms": int(time.time() * 1000),
            "resources": len({resource["resource_id"] for resource in resources}),
            "rows": len(rows),
            "active_methods": active_methods,
            "window": window_info,
            "prediction_skips": list(prediction_skips or []),
        },
        "resources": resources,
        "rows": rows,
    }


def _error_model_metrics(
    kind_metrics: Dict[str, Any], diagnostics: Dict[str, Any],
    active_methods: List[str], window_info: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    evaluation = diagnostics.get("evaluation", {})
    validation = evaluation.get("validation_metrics", {})
    phases = diagnostics.get("phase_failures", {})
    candidates = dict(kind_metrics)
    candidates.update({method: {} for method in validation if method not in candidates})
    for failures in phases.values():
        candidates.update({method: {} for method in failures if method not in candidates})
    window = dict(window_info)
    for key in ("test_start_ms", "test_end_ms", "test_train_end_ms", "routing_train_end_ms"):
        window[key] = evaluation.get(key)
    validation_windows = evaluation.get("validation_windows", [])
    for key, aggregate in (("validation_start_ms", min), ("validation_end_ms", max)):
        values = [entry[key] for entry in validation_windows if entry.get(key) is not None]
        window[key] = aggregate(values) if values else None
    result = {}
    for method in _ordered_methods(candidates, active_methods):
        metric_obj = kind_metrics.get(method, {})
        if not isinstance(metric_obj, dict):
            continue
        # Failed test candidates can still have genuine training-only validation scores.
        values = {**validation.get(method, {}), **metric_obj}
        errors = {key: _json_float(values.get(key)) for key in (
            "rmse", "mae", "mape", "p95_error", "selection_rmse",
            "rolling_rmse", "rolling_mae", "rolling_folds",
            "validation_rmse", "validation_mae", "validation_mape",
            "validation_p95_error", "validation_folds",
        )}
        errors.update(
            window=window, evaluation_role=evaluation.get("role", "legacy_holdout"),
            selection_status=evaluation.get("selection_status", "legacy_unknown"),
            provenance=diagnostics.get("provenance", {}),
            phase_failures={phase: failures[method] for phase, failures in phases.items() if method in failures},
        )
        result[method] = errors
    return result


def _ordered_methods(kind_metrics: Dict[str, Any], active_methods: List[str]) -> List[str]:
    seen = set()
    methods: List[str] = []
    for method in active_methods:
        if method in kind_metrics and method not in seen:
            methods.append(method)
            seen.add(method)
    for method in kind_metrics:
        if method not in seen:
            methods.append(str(method))
            seen.add(str(method))
    return methods


def _json_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if np.isfinite(out) else None
