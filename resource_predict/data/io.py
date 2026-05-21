"""
原始监控数据与预测产物的读写、合并。

- raw_data.json：仅保存观测序列（resource_id / spec / metrics），可长期固定。
- details 分片：保存 charts_forecast（无 y_train/y_test），重新预测时只覆盖这部分。
- API 层将二者合并为前端所需的完整 charts。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from resource_predict.resource_types import metric_names_for_resource, resource_type_of

RAW_SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write text via a sibling temp file, then atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(text, encoding=encoding)
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("failed to clean temp file: %s", tmp_path)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: Optional[int] = None,
    separators: Optional[Tuple[str, str]] = None,
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=ensure_ascii,
            indent=indent,
            separators=separators,
        ),
        encoding="utf-8",
    )


# ---------- 时间戳单位检测（秒 vs 毫秒）---------
# Unix 秒级时间戳当前约为 1.7e9，毫秒级约为 1.7e12
# 使用 1e12 作为分界：
#   < 1e12 → 秒级
#   >= 1e12 → 毫秒级
_TS_UNIT_THRESHOLD: float = 1e12


def _detect_ts_unit(values: List[Any]) -> Optional[str]:
    """通过首个数值推断时间戳单位，返回 's' / 'ms' / None。"""
    if not values:
        return None
    t0 = values[0]
    if isinstance(t0, (int, float)):
        if float(t0) < _TS_UNIT_THRESHOLD:
            logger.debug("根据首值 %.4g 推断时间戳单位为秒 (unit='s')", t0)
            return "s"
        else:
            logger.debug("根据首值 %.4g 推断时间戳单位为毫秒 (unit='ms')", t0)
            return "ms"
    # 非数值（如 ISO 字符串），交由 pd.to_datetime 自动推断
    return None


def coerce_metric_series(metric_data: Any, metric_name: str) -> pd.Series:
    """将外部指标数据统一转换为 pd.Series[DatetimeIndex]。"""
    if isinstance(metric_data, pd.Series):
        s = metric_data.copy()
    elif isinstance(metric_data, dict):
        ts = metric_data.get("timestamps")
        vals = metric_data.get("values")
        if ts is None or vals is None:
            raise ValueError(f"{metric_name} 缺少 timestamps/values 字段")
        if len(ts) == 0:
            raise ValueError(f"{metric_name} timestamps 为空")
        unit = _detect_ts_unit(ts)
        if unit == "s":
            idx = pd.to_datetime(ts, unit="s", errors="coerce")
        elif unit == "ms":
            idx = pd.to_datetime(ts, unit="ms", errors="coerce")
        else:
            idx = pd.to_datetime(ts, errors="coerce")
        s = pd.Series(vals, index=idx, name=metric_name)
    else:
        raise TypeError(
            f"{metric_name} 格式不支持，需为 pd.Series 或 "
            "{'timestamps': [...], 'values': [...]}。"
        )

    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index)
    s = s.sort_index()
    s = pd.Series(pd.to_numeric(s, errors="coerce"), index=s.index, name=metric_name)
    s = s.dropna()
    if s.empty:
        raise ValueError(f"{metric_name} 数据为空或无法转换为数值")
    s = s[~s.index.duplicated(keep="last")]
    return s



def _timestamps_ms_from_index(index: pd.DatetimeIndex) -> List[int]:
    return (index.view("int64") // 1_000_000).tolist()


def _series_to_lists(s: pd.Series) -> List[float]:
    return s.to_numpy(dtype=float).tolist()


def prepared_dict_to_raw_record(p: Dict[str, Any]) -> Dict[str, Any]:
    """将内部 prepared_data 一项转为可 JSON 序列化的原始记录。"""
    metrics: Dict[str, Dict[str, Any]] = {}
    for metric in metric_names_for_resource(p):
        s = p.get(metric)
        if isinstance(s, pd.Series):
            metrics[metric] = {
                "timestamps": _timestamps_ms_from_index(s.index),
                "values": _series_to_lists(s),
            }
    rec: Dict[str, Any] = {
        "resource_id": str(p["resource_id"]),
        "spec": p.get("spec", {}) if isinstance(p.get("spec"), dict) else {},
        "resource_type": resource_type_of(p),
        "metrics": metrics,
    }
    if isinstance(p.get("data_quality"), dict):
        rec["data_quality"] = p["data_quality"]
    return rec


def write_raw_dataset(path: Path, prepared_resources: List[Dict[str, Any]], *, freq: str) -> None:
    payload = {
        "meta": {
            "schema_version": RAW_SCHEMA_VERSION,
            "saved_at_epoch_ms": int(time.time() * 1000),
            "freq": freq,
        },
        "resources": [prepared_dict_to_raw_record(p) for p in prepared_resources],
    }
    atomic_write_json(path, payload, ensure_ascii=False, indent=2)


def read_raw_dataset(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"未找到原始数据文件: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("raw_data.json 根节点必须为 object")
    meta = obj.get("meta", {})
    resources_raw = obj.get("resources", [])
    if not isinstance(resources_raw, list) or not resources_raw:
        raise ValueError("raw_data.json 中 resources 必须为非空 list")
    prepared: List[Dict[str, Any]] = []
    for idx, rec in enumerate(resources_raw):
        if not isinstance(rec, dict):
            continue
        rid = rec.get("resource_id") or f"resource_{idx+1:02d}"
        metrics = rec.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ValueError(f"{rid} 的 metrics 必须为 dict")
        item: Dict[str, Any] = {
            "resource_id": str(rid),
            "spec": rec.get("spec", {}) if isinstance(rec.get("spec"), dict) else {},
        }
        resource_type = str(rec.get("resource_type") or "")
        if resource_type:
            item["resource_type"] = resource_type
        if isinstance(rec.get("data_quality"), dict):
            item["data_quality"] = rec["data_quality"]
        for metric in metric_names_for_resource(item):
            if metric not in metrics:
                raise ValueError(f"{rid} 缺少 {metric} 指标")
            item[metric] = coerce_metric_series(metrics.get(metric), metric)
        prepared.append(item)
    return prepared, meta if isinstance(meta, dict) else {}


def index_prepared_by_id(prepared: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(p["resource_id"]): p for p in prepared}


def merge_charts_into_detail(
    detail: Dict[str, Any],
    raw_by_id: Dict[str, Dict[str, Any]],
    *,
    test_size: int,
) -> Dict[str, Any]:
    """
    将仅含 charts_forecast 的详情记录与原始序列合并为完整 charts。
    若已含完整 charts（含 y_train），则原样返回。
    """
    charts = detail.get("charts")
    if isinstance(charts, dict):
        cpu = charts.get("cpu")
        if isinstance(cpu, dict) and cpu.get("y_train") is not None:
            return detail

    cf = detail.get("charts_forecast")
    if not isinstance(cf, dict):
        return detail

    rid = str(detail.get("resource_id", ""))
    raw = raw_by_id.get(rid)
    if raw is None:
        return detail

    out = {**detail}
    if isinstance(raw.get("spec"), dict) and raw["spec"]:
        out["spec"] = raw["spec"]

    merged_charts: Dict[str, Any] = {}
    for kind in metric_names_for_resource(raw):
        y_full = raw.get(kind)
        if not isinstance(y_full, pd.Series) or y_full.empty:
            continue
        if len(y_full) <= test_size:
            continue
        y_train, y_test = y_full.iloc[:-test_size], y_full.iloc[-test_size:]
        block = cf.get(kind)
        if not isinstance(block, dict):
            continue
        merged_charts[kind] = {
            "x_train_ms": _timestamps_ms_from_index(y_train.index),
            "y_train": _series_to_lists(y_train),
            "x_test_ms": _timestamps_ms_from_index(y_test.index),
            "y_test": _series_to_lists(y_test),
            "preds": block.get("preds", {}),
            "x_pred_ms": block.get("x_pred_ms", []),
            "preds_future": block.get("preds_future", {}),
            "metrics": block.get("metrics", {}),
            "best_method": block.get("best_method", ""),
        }
    out["charts"] = merged_charts
    out.pop("charts_forecast", None)
    return out


def merge_manifest_resources(
    resources: List[Dict[str, Any]],
    raw_by_id: Dict[str, Dict[str, Any]],
    *,
    test_size: int,
) -> List[Dict[str, Any]]:
    return [merge_charts_into_detail(dict(x), raw_by_id, test_size=test_size) for x in resources]

