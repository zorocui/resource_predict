from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from resource_predict.settings import AppConfig, GenerationConfig, settings
from resource_predict.data.io import index_prepared_by_id, merge_charts_into_detail, read_raw_dataset
from resource_predict.services.store.resource_detail import apply_display_window


class ForecastStore:
    """读取 outputs/ 下 summary、details、manifest 与 raw，带 mtime 缓存。"""

    def __init__(
        self,
        app_cfg: Optional[AppConfig] = None,
        generation_cfg: Optional[GenerationConfig] = None,
        *,
        max_details_cache: int = 500,
    ) -> None:
        app_cfg = app_cfg or settings.app
        generation_cfg = generation_cfg or settings.generation
        out_dir = Path(app_cfg.out_dir)
        self._generation_cfg = generation_cfg
        self._display_window_points = int(settings.update.display_window_points)
        self._manifest_path = out_dir / app_cfg.manifest_filename
        self._summary_path = out_dir / app_cfg.summary_index_filename
        self._details_dir = out_dir / app_cfg.details_dirname
        self._raw_path = out_dir / app_cfg.raw_data_filename
        self._max_details_cache = max_details_cache
        self._summary_cache: Dict[str, Any] = {"mtime": 0.0, "data": None}
        self._details_cache: Dict[str, Dict[str, Any]] = {}
        self._raw_cache: Dict[str, Any] = {"mtime": 0.0, "by_id": {}}

    def _try_load_manifest(self) -> Optional[List[dict]]:
        if not self._manifest_path.exists():
            return None
        try:
            obj = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            resources = obj.get("resources", [])
            if isinstance(resources, list) and resources:
                first = resources[0] if resources else None
                if isinstance(first, dict) and first.get("charts"):
                    return resources
                return None
        except Exception:
            return None

    def load_manifest(self) -> List[dict]:
        loaded = self._try_load_manifest()
        return loaded or []

    def _load_summary_index(self) -> Optional[Dict[str, Any]]:
        if not self._summary_path.exists():
            self._summary_cache = {"mtime": 0.0, "data": None}
            return None
        try:
            mtime = int(self._summary_path.stat().st_mtime_ns)
        except OSError:
            return None
        if (
            self._summary_cache.get("data") is not None
            and int(self._summary_cache.get("mtime", 0.0)) == mtime
        ):
            return self._summary_cache["data"]
        try:
            obj = json.loads(self._summary_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        resources = obj.get("resources")
        if not isinstance(resources, list):
            return None
        self._details_cache.clear()
        self._summary_cache = {"mtime": mtime, "data": obj}
        return obj

    def _get_raw_by_id(self) -> Dict[str, Any]:
        if not self._raw_path.exists():
            return {}
        try:
            mtime = int(self._raw_path.stat().st_mtime_ns)
        except OSError:
            return {}
        if self._raw_cache.get("mtime") == mtime and self._raw_cache.get("by_id"):
            return self._raw_cache["by_id"]
        try:
            prepared, _ = read_raw_dataset(self._raw_path)
            by_id = index_prepared_by_id(prepared)
            self._raw_cache["mtime"] = mtime
            self._raw_cache["by_id"] = by_id
            return by_id
        except Exception:
            return {}

    def _build_summary_from_manifest(self) -> Dict[str, Any]:
        resources = self.load_manifest()
        output = []
        for item in resources:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "resource_id": item.get("resource_id"),
                    "resource_type": item.get("resource_type"),
                    "spec": item.get("spec", {}),
                    "best_methods": item.get("best_methods", {}),
                    "metrics": item.get("metrics", {}),
                    "anomaly_score": 0.0,
                    "detail_ref": {},
                    "scaling_advice": item.get("scaling_advice", {}),
                }
            )
        return {"meta": {"resources": len(output)}, "resources": output}

    def get_summary(self) -> Dict[str, Any]:
        summary = self._load_summary_index()
        if summary is not None:
            return summary
        return self._build_summary_from_manifest()

    def _load_details_chunk(self, file_name: str) -> Optional[Dict[str, Any]]:
        if not file_name:
            return None
        if file_name in self._details_cache:
            return self._details_cache[file_name]
        file_path = self._details_dir / file_name
        if not file_path.exists():
            return None
        try:
            obj = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        if len(self._details_cache) >= self._max_details_cache:
            oldest_key = next(iter(self._details_cache))
            del self._details_cache[oldest_key]
        self._details_cache[file_name] = obj
        return obj

    def _resolve_test_size(self, summary_obj: Optional[Dict[str, Any]]) -> int:
        meta = (summary_obj or {}).get("meta", {}) or {}
        raw_ts = meta.get("test_size")
        try:
            return self._generation_cfg.test_size if raw_ts is None else int(raw_ts)
        except Exception:
            return self._generation_cfg.test_size

    def get_resource_detail(self, resource_id: str) -> Optional[Dict[str, Any]]:
        summary = self.get_summary()
        resources = summary.get("resources", [])
        target = None
        for item in resources:
            if isinstance(item, dict) and str(item.get("resource_id")) == resource_id:
                target = item
                break
        if target is None:
            return None

        detail_ref = target.get("detail_ref", {})
        if isinstance(detail_ref, dict) and detail_ref.get("file") is not None:
            chunk = self._load_details_chunk(str(detail_ref.get("file")))
            if isinstance(chunk, dict):
                chunk_resources = chunk.get("resources", [])
                try:
                    offset = int(detail_ref.get("offset"))
                except (TypeError, ValueError):
                    offset = -1
                if isinstance(chunk_resources, list) and 0 <= offset < len(chunk_resources):
                    data = chunk_resources[offset]
                    if isinstance(data, dict):
                        ts = self._resolve_test_size(self._load_summary_index())
                        result = merge_charts_into_detail(
                            data, self._get_raw_by_id(), test_size=ts
                        )
                        apply_display_window(
                            result,
                            test_size=ts,
                            display_window_points=self._display_window_points,
                        )
                        return result

        for item in self.load_manifest():
            if isinstance(item, dict) and str(item.get("resource_id")) == resource_id:
                return item
        return None

