from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Set

import numpy as np

from resource_predict.data.io import atomic_write_json, index_prepared_by_id, merge_manifest_resources
from resource_predict.pipeline.constants import (
    DETAILS_DIRNAME,
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

    total_bytes = 0
    for p in [
        out_base / SUMMARY_INDEX_FILENAME,
        out_base / MANIFEST_FILENAME,
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
    }
    atomic_write_json(
        out_base / GENERATION_STATS_FILENAME,
        stats_payload,
        ensure_ascii=False,
        indent=2,
    )
    return manifest_items
