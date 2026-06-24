from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from resource_predict.data.io import merge_charts_into_detail
from resource_predict.data.raw_store import RawResourceStore
from resource_predict.pipeline.constants import DETAILS_DIRNAME, SUMMARY_INDEX_FILENAME
from resource_predict.pipeline.output_paths import all_scoped_out_dirs
from resource_predict.resource_types import metric_names_for_resource
from resource_predict.settings import AppConfig, GenerationConfig, settings

logger = logging.getLogger(__name__)


class _SingleForecastStore:
    """读取单个 scope 的 summary、预测详情分片和资源级 raw 数据。"""

    def __init__(
        self,
        app_cfg: Optional[AppConfig] = None,
        generation_cfg: Optional[GenerationConfig] = None,
        *,
        max_details_cache: int = 500,
    ) -> None:
        app_cfg = app_cfg or settings.app
        self._generation_cfg = generation_cfg or settings.generation
        self._out_dir = Path(app_cfg.out_dir)
        self._summary_path = self._out_dir / SUMMARY_INDEX_FILENAME
        self._details_dir = self._out_dir / DETAILS_DIRNAME
        self._max_details_cache = max(1, int(max_details_cache))
        self._summary_cache: Dict[str, Any] = {"mtime": 0, "data": None}
        self._summary_by_id: Dict[str, Dict[str, Any]] = {}
        self._details_cache: Dict[str, Dict[str, Any]] = {}
        self._last_summary_cache_hit = False
        self._last_details_cache_hit = False
        self._raw_store = RawResourceStore(
            self._out_dir,
            max_cache_items=int(self._generation_cfg.raw_resource_cache_items),
        )

    def get_summary(self) -> Dict[str, Any]:
        summary = self._load_summary_index()
        return summary if summary is not None else {"meta": {"resources": 0}, "resources": []}

    def has_resource(self, resource_id: str) -> bool:
        self._load_summary_index()
        return str(resource_id) in self._summary_by_id

    def get_resource_detail(
        self,
        resource_id: str,
        *,
        include_charts: bool = True,
        history_points: Optional[int] = None,
        metric: Optional[str] = None,
        container: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        started = time.perf_counter()
        summary = self._load_summary_index()
        if summary is None:
            return None
        target = self._summary_by_id.get(str(resource_id))
        if target is None:
            return None
        detail_started = time.perf_counter()
        data = self._load_detail_item(target)
        detail_elapsed = time.perf_counter() - detail_started
        if data is None:
            return None

        test_size = self._resolve_test_size(summary)
        if not include_charts:
            result = dict(data)
            result["spec"] = target.get("spec", result.get("spec", {}))
            result.pop("charts", None)
            result.pop("charts_forecast", None)
            result.pop("container_charts", None)
            result.pop("container_charts_forecast", None)
            logger.info(
                "[detail] metadata resource_id=%s summary_cache=%s detail_cache=%s detail=%.3fs total=%.3fs",
                resource_id,
                self._last_summary_cache_hit,
                self._last_details_cache_hit,
                detail_elapsed,
                time.perf_counter() - started,
            )
            return result

        points = self._resolve_history_points(history_points)
        raw_started = time.perf_counter()
        raw = self._raw_store.get(resource_id)
        raw_elapsed = time.perf_counter() - raw_started
        if raw is None:
            return None
        allowed_metrics = set(metric_names_for_resource(raw))
        if metric and metric not in allowed_metrics:
            raise ValueError(f"unsupported metric for resource: {metric}")
        if container:
            container_metrics = raw.get("container_metrics")
            if not isinstance(container_metrics, dict):
                raise ValueError("container is only supported for K8S resources with container metrics")
            if container not in container_metrics:
                raise ValueError(f"unknown container for resource: {container}")
        if start_ms is not None and end_ms is not None and int(start_ms) > int(end_ms):
            raise ValueError("start_ms must be less than or equal to end_ms")
        merge_started = time.perf_counter()
        result = merge_charts_into_detail(
            data,
            {str(resource_id): raw},
            test_size=test_size,
            history_points=points,
            metric_filter=metric,
            container_filter=container,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        merge_elapsed = time.perf_counter() - merge_started
        result["chart_window"] = {
            "history_points": points,
            "test_size": test_size,
            "metric": metric or "",
            "container": container or "",
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        point_count, response_bytes = _chart_payload_stats(result)
        logger.info(
            "[detail] charts resource_id=%s summary_cache=%s detail_cache=%s raw_cache=%s "
            "detail=%.3fs raw=%.3fs merge=%.3fs total=%.3fs points=%d bytes=%d history_points=%d metric=%s container=%s",
            resource_id,
            self._last_summary_cache_hit,
            self._last_details_cache_hit,
            self._raw_store.last_cache_hit,
            detail_elapsed,
            raw_elapsed,
            merge_elapsed,
            time.perf_counter() - started,
            point_count,
            response_bytes,
            points,
            metric or "all",
            container or "workload",
        )
        return result

    def get_resource_charts(
        self,
        resource_id: str,
        *,
        history_points: Optional[int] = None,
        metric: Optional[str] = None,
        container: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        detail = self.get_resource_detail(
            resource_id,
            include_charts=True,
            history_points=history_points,
            metric=metric,
            container=container,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if detail is None:
            return None
        return {
            "resource_id": resource_id,
            "resource_type": detail.get("resource_type"),
            "charts": detail.get("charts", {}),
            "container_charts": detail.get("container_charts", {}),
            "chart_window": detail.get("chart_window", {}),
        }

    def _load_summary_index(self) -> Optional[Dict[str, Any]]:
        if not self._summary_path.exists():
            self._summary_cache = {"mtime": 0, "data": None}
            self._summary_by_id = {}
            return None
        try:
            mtime = int(self._summary_path.stat().st_mtime_ns)
        except OSError:
            return None
        if self._summary_cache.get("data") is not None and int(self._summary_cache.get("mtime", 0)) == mtime:
            self._last_summary_cache_hit = True
            return self._summary_cache["data"]
        self._last_summary_cache_hit = False
        try:
            obj = json.loads(self._summary_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[store] 无法读取 summary 索引: %s", self._summary_path)
            return None
        resources = obj.get("resources") if isinstance(obj, dict) else None
        if not isinstance(resources, list):
            return None
        self._summary_by_id = {
            str(item.get("resource_id")): item
            for item in resources
            if isinstance(item, dict) and item.get("resource_id") is not None
        }
        self._details_cache.clear()
        self._summary_cache = {"mtime": mtime, "data": obj}
        return obj

    def _load_detail_item(self, summary_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ref = summary_item.get("detail_ref", {})
        if not isinstance(ref, dict) or not ref.get("file"):
            return None
        chunk = self._load_details_chunk(str(ref["file"]))
        resources = chunk.get("resources", []) if isinstance(chunk, dict) else []
        try:
            offset = int(ref.get("offset"))
        except (TypeError, ValueError):
            return None
        if not isinstance(resources, list) or not (0 <= offset < len(resources)):
            return None
        item = resources[offset]
        return item if isinstance(item, dict) else None

    def _load_details_chunk(self, file_name: str) -> Optional[Dict[str, Any]]:
        if file_name in self._details_cache:
            self._last_details_cache_hit = True
            return self._details_cache[file_name]
        self._last_details_cache_hit = False
        path = self._details_dir / file_name
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[store] 无法读取详情分片: %s", path)
            return None
        if not isinstance(obj, dict):
            return None
        if len(self._details_cache) >= self._max_details_cache:
            self._details_cache.pop(next(iter(self._details_cache)))
        self._details_cache[file_name] = obj
        return obj

    def _resolve_test_size(self, summary: Dict[str, Any]) -> int:
        meta = summary.get("meta", {}) if isinstance(summary, dict) else {}
        value = meta.get("test_size") if isinstance(meta, dict) else None
        try:
            return int(value) if value is not None else int(self._generation_cfg.default_test_size)
        except (TypeError, ValueError):
            return int(self._generation_cfg.default_test_size)

    def _resolve_history_points(self, value: Optional[int]) -> int:
        points = (
            int(self._generation_cfg.detail_history_points_default)
            if value is None
            else int(value)
        )
        maximum = int(self._generation_cfg.detail_history_points_max)
        if points <= 0 or points > maximum:
            raise ValueError(f"history_points must be between 1 and {maximum}")
        return points


class ForecastStore:
    """合并 VM/K8S scope，并为详情请求路由到唯一资源存储。"""

    def __init__(
        self,
        app_cfg: Optional[AppConfig] = None,
        generation_cfg: Optional[GenerationConfig] = None,
        *,
        max_details_cache: int = 500,
    ) -> None:
        app_cfg = app_cfg or settings.app
        generation_cfg = generation_cfg or settings.generation
        bases = [path for _scope, path in all_scoped_out_dirs(Path(app_cfg.out_dir))]
        per_reader_cache = max(1, max_details_cache // max(1, len(bases)))
        self._readers = [
            _SingleForecastStore(
                AppConfig(
                    static_folder=app_cfg.static_folder,
                    template_folder=app_cfg.template_folder,
                    out_dir=str(base),
                    log_file=app_cfg.log_file,
                    log_level=app_cfg.log_level,
                    log_console=app_cfg.log_console,
                    host=app_cfg.host,
                    port=app_cfg.port,
                    debug=app_cfg.debug,
                ),
                generation_cfg,
                max_details_cache=per_reader_cache,
            )
            for base in bases
        ]

    def get_summary(self) -> Dict[str, Any]:
        resources: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for reader in self._readers:
            for item in reader.get_summary().get("resources", []):
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("resource_id") or "")
                if rid and rid not in seen:
                    seen.add(rid)
                    resources.append(item)
        return {
            "meta": {"resources": len(resources), "output_mode": "scoped"},
            "resources": resources,
        }

    def get_resource_detail(self, resource_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        reader = self._reader_for(resource_id)
        return reader.get_resource_detail(resource_id, **kwargs) if reader is not None else None

    def get_resource_charts(self, resource_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        reader = self._reader_for(resource_id)
        return reader.get_resource_charts(resource_id, **kwargs) if reader is not None else None

    def _reader_for(self, resource_id: str) -> Optional[_SingleForecastStore]:
        for reader in self._readers:
            if reader.has_resource(resource_id):
                return reader
        return None


def _chart_payload_stats(detail: Dict[str, Any]) -> tuple[int, int]:
    points = 0
    for charts in (detail.get("charts", {}),):
        if not isinstance(charts, dict):
            continue
        for block in charts.values():
            if isinstance(block, dict):
                points += sum(
                    len(block.get(key, []))
                    for key in ("y_train", "y_test")
                    if isinstance(block.get(key), list)
                )
    containers = detail.get("container_charts", {})
    if isinstance(containers, dict):
        for metrics in containers.values():
            if not isinstance(metrics, dict):
                continue
            for block in metrics.values():
                if isinstance(block, dict):
                    points += sum(
                        len(block.get(key, []))
                        for key in ("y_train", "y_test")
                        if isinstance(block.get(key), list)
                    )
    payload = {
        "charts": detail.get("charts", {}),
        "container_charts": detail.get("container_charts", {}),
        "chart_window": detail.get("chart_window", {}),
    }
    response_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return points, response_bytes
