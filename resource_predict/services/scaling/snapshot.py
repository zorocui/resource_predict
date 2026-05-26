from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict

import numpy as np

from resource_predict.core.decision import build_scaling_advice
from resource_predict.data.io import atomic_write_json
from resource_predict.pipeline.constants import (
    DETAILS_DIRNAME,
    MANIFEST_FILENAME,
    METRIC_NAMES,
    RAW_DATA_FILENAME,
    SUMMARY_INDEX_FILENAME,
)
from resource_predict.pipeline.output_paths import scoped_out_dir
from resource_predict.settings import settings


logger = logging.getLogger(__name__)
_LOCK = threading.RLock()


def apply_scaling_success_snapshot(plan: Any) -> Dict[str, Any]:
    resource_id = str(getattr(plan, "resource_id", "") or "").strip()
    if not resource_id:
        raise ValueError("scaling plan is missing resource_id")

    effective_spec = _effective_spec(plan)
    _validate_effective_spec(plan, effective_spec)
    resource_type = str(getattr(plan, "resource_type", "") or "").lower()
    out_dir = scoped_out_dir("k8s" if resource_type.startswith("k8s") else "vm", settings.app.out_dir)
    summary_path = out_dir / SUMMARY_INDEX_FILENAME
    details_dir = out_dir / DETAILS_DIRNAME
    raw_path = out_dir / RAW_DATA_FILENAME
    manifest_path = out_dir / MANIFEST_FILENAME

    updated: Dict[str, Any] = {
        "resource_id": resource_id,
        "effective_spec": effective_spec,
        "summary_updated": False,
        "detail_updated": False,
        "raw_updated": False,
        "manifest_updated": False,
        "advice_recomputed": False,
    }

    with _LOCK:
        detail_item = _update_detail(details_dir, summary_path, resource_id, effective_spec, updated)
        _update_summary(summary_path, resource_id, effective_spec, detail_item, updated)
        _update_raw(raw_path, resource_id, effective_spec, updated)
        _update_manifest(manifest_path, resource_id, effective_spec, detail_item, updated)

    logger.info(
        "[scaling] local snapshot updated: resource_id=%s summary=%s detail=%s raw=%s manifest=%s advice_recomputed=%s",
        resource_id,
        updated["summary_updated"],
        updated["detail_updated"],
        updated["raw_updated"],
        updated["manifest_updated"],
        updated["advice_recomputed"],
    )
    return updated


def _effective_spec(plan: Any) -> Dict[str, Any]:
    target = dict(getattr(plan, "target_spec", {}) or {})
    details = getattr(plan, "details", {}) or {}
    selected = details.get("selected_flavor", {}) if isinstance(details, dict) else {}
    effective = dict(target)
    if isinstance(selected, dict):
        for src, dst in (("cpu_cores", "cpu_cores"), ("memory_gb", "memory_gb"), ("disk_gb", "disk_gb")):
            if selected.get(src) is not None:
                effective[dst] = selected[src]
        if selected.get("name"):
            effective["flavor"] = selected["name"]
            effective["target_flavor"] = selected["name"]
    effective["last_scaled_at_epoch_ms"] = int(time.time() * 1000)
    return effective


def _validate_effective_spec(plan: Any, effective_spec: Dict[str, Any]) -> None:
    details = getattr(plan, "details", {}) or {}
    is_openstack = isinstance(details, dict) and bool(details.get("instance_id"))
    if not is_openstack:
        return
    missing = [
        key
        for key in ("cpu_cores", "memory_gb", "disk_gb")
        if effective_spec.get(key) is None
    ]
    if missing:
        raise ValueError(
            "local snapshot is missing effective OpenStack spec fields: "
            + ", ".join(missing)
            + "; ensure target_spec includes CPU/memory/disk or the target flavor can be discovered"
        )


def _merge_spec(current: Any, effective: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(current) if isinstance(current, dict) else {}
    for key, value in effective.items():
        if value is not None:
            base[key] = value
    return base


def _recompute_advice(item: Dict[str, Any]) -> bool:
    charts = item.get("charts_forecast", {})
    if not isinstance(charts, dict):
        return False
    future_by_metric: Dict[str, np.ndarray] = {}
    for metric in METRIC_NAMES:
        block = charts.get(metric, {})
        if not isinstance(block, dict):
            return False
        best = str(block.get("best_method") or "")
        futures = block.get("preds_future", {})
        if not best or not isinstance(futures, dict) or best not in futures:
            return False
        future_by_metric[metric] = np.asarray(futures.get(best) or [], dtype=float)
    item["scaling_advice"] = build_scaling_advice(future_by_metric, current_spec=item.get("spec", {}))
    return True


def _read_json(path: Path) -> Any:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _update_detail(
    details_dir: Path,
    summary_path: Path,
    resource_id: str,
    effective_spec: Dict[str, Any],
    updated: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not summary_path.exists():
        return None
    summary = _read_json(summary_path)
    if not isinstance(summary, dict):
        return None
    resources = summary.get("resources", [])
    if not isinstance(resources, list):
        return None
    ref = None
    for row in resources:
        if isinstance(row, dict) and str(row.get("resource_id")) == resource_id:
            ref = row.get("detail_ref", {})
            break
    if not isinstance(ref, dict) or not ref.get("file"):
        return None
    try:
        offset = int(ref.get("offset"))
    except Exception:
        return None

    detail_path = details_dir / str(ref.get("file"))
    if not detail_path.exists():
        return None
    chunk = _read_json(detail_path)
    chunk_resources = chunk.get("resources", []) if isinstance(chunk, dict) else []
    if not isinstance(chunk_resources, list) or not (0 <= offset < len(chunk_resources)):
        return None
    item = chunk_resources[offset]
    if not isinstance(item, dict):
        return None
    item["spec"] = _merge_spec(item.get("spec", {}), effective_spec)
    if _recompute_advice(item):
        updated["advice_recomputed"] = True
    atomic_write_json(detail_path, chunk, ensure_ascii=False, separators=(",", ":"))
    updated["detail_updated"] = True
    return item


def _update_summary(
    summary_path: Path,
    resource_id: str,
    effective_spec: Dict[str, Any],
    detail_item: Dict[str, Any] | None,
    updated: Dict[str, Any],
) -> None:
    if not summary_path.exists():
        return
    summary = _read_json(summary_path)
    resources = summary.get("resources", []) if isinstance(summary, dict) else []
    if not isinstance(resources, list):
        return
    for row in resources:
        if not isinstance(row, dict) or str(row.get("resource_id")) != resource_id:
            continue
        row["spec"] = _merge_spec(row.get("spec", {}), effective_spec)
        if isinstance(detail_item, dict) and isinstance(detail_item.get("scaling_advice"), dict):
            row["scaling_advice"] = detail_item["scaling_advice"]
        row["last_scaling_snapshot_at_epoch_ms"] = int(time.time() * 1000)
        updated["summary_updated"] = True
        break
    if updated["summary_updated"]:
        atomic_write_json(summary_path, summary, ensure_ascii=False, separators=(",", ":"))


def _update_raw(
    raw_path: Path,
    resource_id: str,
    effective_spec: Dict[str, Any],
    updated: Dict[str, Any],
) -> None:
    if not raw_path.exists():
        return
    raw = _read_json(raw_path)
    resources = raw.get("resources", []) if isinstance(raw, dict) else []
    if not isinstance(resources, list):
        return
    for row in resources:
        if not isinstance(row, dict) or str(row.get("resource_id")) != resource_id:
            continue
        row["spec"] = _merge_spec(row.get("spec", {}), effective_spec)
        updated["raw_updated"] = True
        break
    if updated["raw_updated"]:
        meta = raw.get("meta", {})
        if isinstance(meta, dict):
            meta["updated_at_epoch_ms"] = int(time.time() * 1000)
            raw["meta"] = meta
        atomic_write_json(raw_path, raw, ensure_ascii=False, indent=2)


def _update_manifest(
    manifest_path: Path,
    resource_id: str,
    effective_spec: Dict[str, Any],
    detail_item: Dict[str, Any] | None,
    updated: Dict[str, Any],
) -> None:
    if not manifest_path.exists():
        return
    manifest = _read_json(manifest_path)
    resources = manifest.get("resources", []) if isinstance(manifest, dict) else []
    if not isinstance(resources, list):
        return
    for row in resources:
        if not isinstance(row, dict) or str(row.get("resource_id")) != resource_id:
            continue
        row["spec"] = _merge_spec(row.get("spec", {}), effective_spec)
        if isinstance(detail_item, dict) and isinstance(detail_item.get("scaling_advice"), dict):
            row["scaling_advice"] = detail_item["scaling_advice"]
        updated["manifest_updated"] = True
        break
    if updated["manifest_updated"]:
        atomic_write_json(manifest_path, manifest, ensure_ascii=False, indent=2)
