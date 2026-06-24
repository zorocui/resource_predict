from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from resource_predict.data.io import (
    atomic_write_json,
    atomic_write_text,
    prepared_dict_to_raw_record,
    raw_record_to_prepared,
)
from resource_predict.resource_types import metric_names_for_resource, resource_type_of

logger = logging.getLogger(__name__)

RAW_INDEX_SCHEMA_VERSION = 2
RAW_INDEX_FILENAME = "raw_index.json"
RAW_RESOURCES_DIRNAME = "raw"
RAW_STALE_FILE_GRACE_SECONDS = 300


class RawResourceStore:
    """基于 raw_index.json 定位并读取单个资源的原始指标。"""

    def __init__(self, out_base: Path, *, max_cache_items: int = 100) -> None:
        self.out_base = Path(out_base)
        self.index_path = self.out_base / RAW_INDEX_FILENAME
        self.max_cache_items = max(1, int(max_cache_items))
        self._index_mtime_ns = 0
        self._index: Dict[str, Any] | None = None
        self._cache: OrderedDict[str, Tuple[str, Dict[str, Any]]] = OrderedDict()
        self.last_cache_hit = False

    def exists(self) -> bool:
        return self.index_path.exists()

    def metadata(self) -> Dict[str, Any]:
        index = self._load_index()
        return {
            key: value
            for key, value in index.items()
            if key not in {"resources"}
        }

    def resource_ids(self) -> List[str]:
        return list(self._resource_refs().keys())

    def get(self, resource_id: str) -> Optional[Dict[str, Any]]:
        rid = str(resource_id)
        ref = self._resource_refs().get(rid)
        if not isinstance(ref, dict):
            return None
        relative_file = str(ref.get("file") or "")
        cached = self._cache.get(rid)
        if cached is not None and cached[0] == relative_file:
            self.last_cache_hit = True
            self._cache.move_to_end(rid)
            return cached[1]
        self.last_cache_hit = False
        path = _resolve_raw_path(self.out_base, relative_file)
        try:
            text = path.read_text(encoding="utf-8")
            _validate_raw_file_hashes(rid, path, text)
            record = json.loads(text)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"raw 索引引用的资源文件不存在: {relative_file}") from exc
        if not isinstance(record, dict) or str(record.get("resource_id") or "") != rid:
            raise ValueError(f"raw 资源文件与索引不一致: {relative_file}")
        prepared = raw_record_to_prepared(record)
        self._cache[rid] = (relative_file, prepared)
        self._cache.move_to_end(rid)
        while len(self._cache) > self.max_cache_items:
            self._cache.popitem(last=False)
        return prepared

    def read_many(self, resource_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        ids = list(resource_ids) if resource_ids is not None else self.resource_ids()
        items: List[Dict[str, Any]] = []
        for resource_id in ids:
            item = self.get(str(resource_id))
            if item is not None:
                items.append(item)
        return items

    def raw_ref(self, resource_id: str) -> Optional[Dict[str, Any]]:
        ref = self._resource_refs().get(str(resource_id))
        return dict(ref) if isinstance(ref, dict) else None

    def _resource_refs(self) -> Dict[str, Any]:
        resources = self._load_index().get("resources", {})
        if not isinstance(resources, dict):
            raise ValueError(f"{self.index_path} 的 resources 必须为 object")
        return resources

    def _load_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            raise FileNotFoundError(f"未找到原始数据索引: {self.index_path}")
        mtime_ns = int(self.index_path.stat().st_mtime_ns)
        if self._index is not None and self._index_mtime_ns == mtime_ns:
            return self._index
        obj = json.loads(self.index_path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            raise ValueError(f"{self.index_path} 根节点必须为 object")
        if int(obj.get("schema_version", 0)) != RAW_INDEX_SCHEMA_VERSION:
            raise ValueError(
                f"{self.index_path} schema_version 必须为 {RAW_INDEX_SCHEMA_VERSION}，"
                "请删除旧产物后重新生成"
            )
        resources = obj.get("resources")
        if not isinstance(resources, dict) or not resources:
            raise ValueError(f"{self.index_path} 中 resources 必须为非空 object")
        self._index = obj
        self._index_mtime_ns = mtime_ns
        for rid, (relative_file, _item) in list(self._cache.items()):
            current_ref = resources.get(rid)
            current_file = str(current_ref.get("file") or "") if isinstance(current_ref, dict) else ""
            if relative_file != current_file:
                self._cache.pop(rid, None)
        return obj


def write_raw_resource_dataset(
    out_base: Path,
    prepared_resources: Sequence[Dict[str, Any]],
    *,
    freq: str,
    changed_resource_ids: Optional[Iterable[str]] = None,
) -> Dict[str, int]:
    """原子提交一份 raw 资源索引；指定 changed IDs 时复用其他资源引用。"""
    base = Path(out_base)
    prepared_by_id = {
        str(item.get("resource_id")): item
        for item in prepared_resources
        if isinstance(item, dict) and item.get("resource_id") is not None
    }
    if not prepared_by_id:
        raise ValueError("不能写入空 raw 资源数据集")

    old_index = _read_existing_index(base)
    old_resources = old_index.get("resources", {}) if isinstance(old_index, dict) else {}
    if not isinstance(old_resources, dict):
        old_resources = {}
    changed = None if changed_resource_ids is None else {str(x) for x in changed_resource_ids}
    if changed is not None:
        missing_changed = changed - set(prepared_by_id)
        if missing_changed:
            raise ValueError(
                "changed_resource_ids 缺少对应资源数据: "
                + ", ".join(sorted(missing_changed))
            )
    now_ms = int(time.time() * 1000)
    new_resources: Dict[str, Dict[str, Any]] = (
        {
            str(rid): dict(ref)
            for rid, ref in old_resources.items()
            if isinstance(ref, dict) and (changed is None or str(rid) not in changed)
        }
        if changed is not None
        else {}
    )
    written = 0
    reused = 0

    for rid, item in prepared_by_id.items():
        old_ref = old_resources.get(rid)
        if changed is not None and rid not in changed and isinstance(old_ref, dict):
            old_path = _resolve_raw_path(base, str(old_ref.get("file") or ""))
            if not old_path.exists():
                raise FileNotFoundError(f"未变化资源的 raw 文件不存在: {old_path}")
            new_resources[rid] = dict(old_ref)
            reused += 1
            continue
        record = prepared_dict_to_raw_record(item)
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        resource_hash = hashlib.sha256(rid.encode("utf-8")).hexdigest()
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        relative = PurePosixPath(
            RAW_RESOURCES_DIRNAME,
            resource_hash[:2],
            f"{resource_hash}-{content_hash}.json",
        ).as_posix()
        path = _resolve_raw_path(base, relative)
        if not path.exists():
            atomic_write_text(path, text, encoding="utf-8")
            written += 1
        else:
            reused += 1
        lengths = [
            len(item[metric])
            for metric in metric_names_for_resource(item)
            if hasattr(item.get(metric), "__len__")
        ]
        new_resources[rid] = {
            "file": relative,
            "resource_type": resource_type_of(item),
            "points": min(lengths) if lengths else 0,
            "updated_at_epoch_ms": now_ms,
        }

    payload = {
        "schema_version": RAW_INDEX_SCHEMA_VERSION,
        "generated_at_epoch_ms": now_ms,
        "freq": str(freq),
        "resources": new_resources,
    }
    atomic_write_json(base / RAW_INDEX_FILENAME, payload, ensure_ascii=False, separators=(",", ":"))
    index_bytes = int((base / RAW_INDEX_FILENAME).stat().st_size)

    old_files = {
        str(ref.get("file"))
        for ref in old_resources.values()
        if isinstance(ref, dict) and ref.get("file")
    }
    new_files = {str(ref["file"]) for ref in new_resources.values()}
    removed = _remove_unreferenced_files(base, old_files - new_files)
    # 上一次提交中处于安全宽限期的旧分片，不再出现在当前 old_files 中；
    # 每轮提交都扫描一次孤立分片，保证纯增量运行也能最终回收它们。
    removed += _remove_orphan_raw_files(base, new_files)
    return {
        "resources": len(new_resources),
        "files_total": len(new_files),
        "files_written": written,
        "files_reused": reused,
        "files_removed": removed,
        "index_bytes": index_bytes,
    }


def _read_existing_index(out_base: Path) -> Dict[str, Any]:
    path = out_base / RAW_INDEX_FILENAME
    if not path.exists():
        return {}
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or int(obj.get("schema_version", 0)) != RAW_INDEX_SCHEMA_VERSION:
        raise ValueError(f"{path} 不是当前 raw 索引格式，请删除旧产物后重新生成")
    return obj


def _resolve_raw_path(out_base: Path, relative_file: str) -> Path:
    relative = PurePosixPath(str(relative_file))
    if not relative_file or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"非法 raw 资源路径: {relative_file!r}")
    if not relative.parts or relative.parts[0] != RAW_RESOURCES_DIRNAME:
        raise ValueError(f"raw 资源路径必须位于 {RAW_RESOURCES_DIRNAME}/: {relative_file!r}")
    base = out_base.resolve()
    path = (base / Path(*relative.parts)).resolve()
    if path != base and base not in path.parents:
        raise ValueError(f"raw 资源路径越界: {relative_file!r}")
    return path


def _remove_unreferenced_files(out_base: Path, relative_files: Iterable[str]) -> int:
    removed = 0
    for relative_file in relative_files:
        try:
            path = _resolve_raw_path(out_base, relative_file)
            if path.exists() and _old_enough_to_remove(path):
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("无法清理旧 raw 资源文件 %s: %s", relative_file, exc)
    return removed


def _remove_orphan_raw_files(out_base: Path, referenced: set[str]) -> int:
    raw_dir = out_base / RAW_RESOURCES_DIRNAME
    if not raw_dir.exists():
        return 0
    removed = 0
    for path in raw_dir.rglob("*.json"):
        relative = path.relative_to(out_base).as_posix()
        if relative in referenced:
            continue
        if not _old_enough_to_remove(path):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("无法清理孤立 raw 资源文件 %s: %s", path, exc)
    for directory in sorted((p for p in raw_dir.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def _old_enough_to_remove(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime >= RAW_STALE_FILE_GRACE_SECONDS
    except OSError:
        return False


def _validate_raw_file_hashes(resource_id: str, path: Path, text: str) -> None:
    resource_hash = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    expected_name = f"{resource_hash}-{content_hash}.json"
    if path.parent.name != resource_hash[:2] or path.name != expected_name:
        raise ValueError(
            f"raw 资源文件哈希不匹配: {path.name}，期望 {expected_name}"
        )
