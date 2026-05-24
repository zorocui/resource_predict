from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np

from resource_predict.pipeline.constants import DETAILS_DIRNAME, SUMMARY_INDEX_FILENAME
from resource_predict.core.decision import build_scaling_advice
from resource_predict.core.k8s_pod_decision import build_k8s_pod_advice
from resource_predict.resource_types import METRIC_NAMES, metric_names_for_resource, resource_type_of

logger = logging.getLogger(__name__)


def load_existing_forecast_items(out_base: Path) -> List[Dict[str, Any]]:
    summary_path = out_base / SUMMARY_INDEX_FILENAME
    details_dir = out_base / DETAILS_DIRNAME
    if not summary_path.exists():
        return []
    try:
        summary_obj = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[progress] 读取既有 summary_index 失败，将只写本次预测资源: %s", exc)
        return []
    if not isinstance(summary_obj, dict):
        return []
    resources = summary_obj.get("resources", [])
    if not isinstance(resources, list):
        return []

    chunk_cache: Dict[str, Dict[str, Any]] = {}
    items: List[Dict[str, Any]] = []
    for row in resources:
        if not isinstance(row, dict):
            continue
        ref = row.get("detail_ref", {})
        if not isinstance(ref, dict):
            continue
        file_name = str(ref.get("file") or "")
        if not file_name:
            continue
        try:
            offset = int(ref.get("offset"))
        except Exception:
            continue
        chunk = chunk_cache.get(file_name)
        if chunk is None:
            try:
                obj = json.loads((details_dir / file_name).read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("[progress] 读取既有详情分片 %s 失败: %s", file_name, exc)
                continue
            if not isinstance(obj, dict):
                continue
            chunk_cache[file_name] = obj
            chunk = obj
        chunk_resources = chunk.get("resources", [])
        if isinstance(chunk_resources, list) and 0 <= offset < len(chunk_resources):
            item = chunk_resources[offset]
            if isinstance(item, dict) and item.get("resource_id") is not None:
                items.append(dict(item))
    return items


def merge_partial_forecast_items(
    existing_items: List[Dict[str, Any]],
    updated_items: List[Dict[str, Any]],
    *,
    metric_names_by_resource: Optional[Dict[str, Set[str]]] = None,
) -> List[Dict[str, Any]]:
    metric_filter = metric_names_by_resource or {}
    updated_by_id = {str(item.get("resource_id")): item for item in updated_items}

    def _rebuild_advice(item: Dict[str, Any]) -> None:
        charts = item.get("charts_forecast", {})
        if not isinstance(charts, dict):
            return
        future_by_metric: Dict[str, np.ndarray] = {}
        metric_names = metric_names_for_resource(item)
        for metric in metric_names:
            block = charts.get(metric, {})
            if not isinstance(block, dict):
                return
            best = str(block.get("best_method") or "")
            futures = block.get("preds_future", {})
            if not best or not isinstance(futures, dict) or best not in futures:
                return
            future_by_metric[metric] = np.asarray(futures.get(best) or [], dtype=float)
        if resource_type_of(item) in {"k8s_pod", "k8s_workload"} and len(future_by_metric) == len(metric_names):
            item["scaling_advice"] = build_k8s_pod_advice(
                future_by_metric,
                resource=item,
            )
        elif len(future_by_metric) == len(METRIC_NAMES):
            item["scaling_advice"] = build_scaling_advice(
                future_by_metric,
                current_spec=item.get("spec", {}),
            )

    def _merge_one(old: Dict[str, Any], new: Dict[str, Any], metrics: Set[str]) -> Dict[str, Any]:
        if not metrics:
            return new
        merged = dict(old)
        if isinstance(new.get("spec"), dict):
            merged["spec"] = new.get("spec", {})
        for field in ("best_methods", "metrics", "charts_forecast"):
            old_obj = old.get(field, {})
            new_obj = new.get(field, {})
            if not isinstance(old_obj, dict) or not isinstance(new_obj, dict):
                continue
            field_out = dict(old_obj)
            for metric in metrics:
                if metric in new_obj:
                    field_out[metric] = new_obj[metric]
            merged[field] = field_out
        _rebuild_advice(merged)
        return merged

    merged: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in existing_items:
        rid = str(item.get("resource_id"))
        if not rid or rid == "None":
            continue
        updated = updated_by_id.get(rid)
        if updated is None:
            merged.append(item)
        else:
            merged.append(_merge_one(item, updated, metric_filter.get(rid, set())))
        seen.add(rid)
    for item in updated_items:
        rid = str(item.get("resource_id"))
        if rid and rid not in seen:
            merged.append(item)
    return merged
