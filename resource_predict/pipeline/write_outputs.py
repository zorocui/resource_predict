from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Set

import numpy as np

from resource_predict.data.io import atomic_write_json, index_prepared_by_id, merge_manifest_resources
from resource_predict.pipeline.constants import (
    DETAILS_DIRNAME,
    FORECAST_ERROR_REPORT_FILENAME,
    GENERATION_STATS_FILENAME,
    MANIFEST_FILENAME,
    RAW_DATA_FILENAME,
    SUMMARY_INDEX_FILENAME,
)
from resource_predict.resource_types import metric_names_for_resource, resource_type_of


def write_prediction_outputs(
    *,
    out_base: Path,
    resources_items: List[Dict[str, Any]],
    prepared_data: List[Dict[str, Any]],
    raw_prepared_data: List[Dict[str, Any]] | None,
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
) -> List[Dict[str, Any]]:
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
            "raw_data_file": RAW_DATA_FILENAME,
        },
        "resources": summary_resources,
    }
    atomic_write_json(
        out_base / SUMMARY_INDEX_FILENAME,
        summary_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    raw_by_id = index_prepared_by_id(raw_prepared_data or prepared_data)
    manifest_items = merge_manifest_resources(resources_items, raw_by_id, test_size=test_size)
    atomic_write_json(
        out_base / MANIFEST_FILENAME,
        {"resources": manifest_items},
        ensure_ascii=False,
        indent=2,
    )
    error_report = _build_forecast_error_report(
        resources_items=resources_items,
        active_methods=active_methods,
        test_size=test_size,
        future_steps=future_steps,
        forecast_window=forecast_window,
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
        metrics_out: Dict[str, Dict[str, Any]] = {}
        metrics_by_kind = item.get("metrics", {})
        if not isinstance(metrics_by_kind, dict):
            continue
        for metric in metric_names_for_resource(item):
            kind_metrics = metrics_by_kind.get(metric, {})
            if not isinstance(kind_metrics, dict):
                continue
            model_metrics: Dict[str, Dict[str, Any]] = {}
            methods = _ordered_methods(kind_metrics, active_methods)
            for method in methods:
                metric_obj = kind_metrics.get(method, {})
                if not isinstance(metric_obj, dict):
                    continue
                errors = {
                    "rmse": _json_float(metric_obj.get("rmse")),
                    "mae": _json_float(metric_obj.get("mae")),
                    "mape": _json_float(metric_obj.get("mape")),
                    "p95_error": _json_float(metric_obj.get("p95_error")),
                    "selection_rmse": _json_float(metric_obj.get("selection_rmse")),
                    "rolling_rmse": _json_float(metric_obj.get("rolling_rmse")),
                    "rolling_mae": _json_float(metric_obj.get("rolling_mae")),
                    "rolling_folds": _json_float(metric_obj.get("rolling_folds")),
                    "window": window_info,
                }
                model_metrics[method] = errors
                rows.append(
                    {
                        "resource_id": rid,
                        "resource_type": rtype,
                        "metric": metric,
                        "model": method,
                        **errors,
                    }
                )
            if model_metrics:
                metrics_out[metric] = model_metrics
        if metrics_out:
            resources.append(
                {
                    "resource_id": rid,
                    "resource_type": rtype,
                    "metrics": metrics_out,
                }
            )
    return {
        "meta": {
            "generated_at_epoch_ms": int(time.time() * 1000),
            "resources": len(resources),
            "rows": len(rows),
            "active_methods": active_methods,
            "window": window_info,
        },
        "resources": resources,
        "rows": rows,
    }


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
