"""
监控数据序列化与预测产物合并。

- raw_index.json + raw/：由 data/raw_store.py 保存资源级观测分片。
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

import pandas as pd

from resource_predict.resource_types import metric_names_for_resource, resource_type_of

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
    container_metrics = _serialize_container_metric_series(p.get("container_metrics"))
    if container_metrics:
        rec["container_metrics"] = container_metrics
    if isinstance(p.get("container_data_quality"), dict):
        rec["container_data_quality"] = p["container_data_quality"]
    if isinstance(p.get("container_metric_modes"), dict):
        rec["container_metric_modes"] = p["container_metric_modes"]
    return rec


def _serialize_container_metric_series(value: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for container, metrics in value.items():
        name = str(container or "").strip()
        if not name or not isinstance(metrics, dict):
            continue
        metric_out: Dict[str, Dict[str, Any]] = {}
        for metric, series in metrics.items():
            if not isinstance(series, pd.Series):
                continue
            metric_out[str(metric)] = {
                "timestamps": _timestamps_ms_from_index(series.index),
                "values": _series_to_lists(series),
            }
        if metric_out:
            out[name] = metric_out
    return out


def raw_record_to_prepared(rec: Dict[str, Any]) -> Dict[str, Any]:
    """将一个资源级 raw JSON 记录转换为内部 pandas 结构。"""
    if not isinstance(rec, dict):
        raise TypeError("raw resource record 必须为 object")
    rid = str(rec.get("resource_id") or "").strip()
    if not rid:
        raise ValueError("raw resource record 缺少 resource_id")
    metrics = rec.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError(f"{rid} 的 metrics 必须为 dict")
    item: Dict[str, Any] = {
        "resource_id": rid,
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
    container_metrics = _coerce_container_metric_series(
        rec.get("container_metrics"),
        metric_names_for_resource(item),
    )
    if container_metrics:
        item["container_metrics"] = container_metrics
    if isinstance(rec.get("container_data_quality"), dict):
        item["container_data_quality"] = rec["container_data_quality"]
    if isinstance(rec.get("container_metric_modes"), dict):
        item["container_metric_modes"] = rec["container_metric_modes"]
    return item


def _coerce_container_metric_series(value: Any, metric_names: Tuple[str, ...]) -> Dict[str, Dict[str, pd.Series]]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Dict[str, pd.Series]] = {}
    for container, metrics in value.items():
        name = str(container or "").strip()
        if not name or not isinstance(metrics, dict):
            continue
        metric_out: Dict[str, pd.Series] = {}
        for metric in metric_names:
            if metric not in metrics:
                continue
            metric_out[metric] = coerce_metric_series(metrics.get(metric), f"{name}.{metric}")
        if metric_out:
            out[name] = metric_out
    return out


def merge_charts_into_detail(
    detail: Dict[str, Any],
    raw_by_id: Dict[str, Dict[str, Any]],
    *,
    test_size: int,
    history_points: Optional[int] = None,
    metric_filter: Optional[str] = None,
    container_filter: Optional[str] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    将仅含 charts_forecast 的详情记录与原始序列合并为完整 charts。
    若已含完整 charts（含 y_train），则原样返回。
    """
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
        if metric_filter and kind != metric_filter:
            continue
        y_full = raw.get(kind)
        if not isinstance(y_full, pd.Series) or y_full.empty:
            continue
        if len(y_full) <= test_size:
            continue
        y_train, y_test = y_full.iloc[:-test_size], y_full.iloc[-test_size:]
        y_train = _filter_series_time_range(y_train, start_ms=start_ms, end_ms=end_ms)
        if history_points is not None:
            points = max(0, int(history_points))
            y_train = y_train.iloc[-points:] if points else y_train.iloc[0:0]
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
    container_charts = _merge_container_charts(
        raw,
        detail,
        test_size=test_size,
        history_points=history_points,
        metric_filter=metric_filter,
        container_filter=container_filter,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if container_charts:
        out["container_charts"] = container_charts
    if isinstance(raw.get("container_data_quality"), dict):
        out["container_data_quality"] = raw["container_data_quality"]
    if isinstance(raw.get("container_metric_modes"), dict):
        out["container_metric_modes"] = raw["container_metric_modes"]
    out.pop("charts_forecast", None)
    out.pop("container_charts_forecast", None)
    return out


def _merge_container_charts(
    raw: Dict[str, Any],
    detail: Dict[str, Any],
    *,
    test_size: int,
    history_points: Optional[int] = None,
    metric_filter: Optional[str] = None,
    container_filter: Optional[str] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    raw_container_metrics = raw.get("container_metrics")
    forecast_container_metrics = detail.get("container_charts_forecast")
    if not isinstance(raw_container_metrics, dict) or not isinstance(forecast_container_metrics, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for container, metrics in raw_container_metrics.items():
        if container_filter and str(container) != container_filter:
            continue
        if not isinstance(metrics, dict):
            continue
        forecast_metrics = forecast_container_metrics.get(container, {})
        if not isinstance(forecast_metrics, dict):
            continue
        metric_out: Dict[str, Any] = {}
        for metric, y_full in metrics.items():
            if metric_filter and str(metric) != metric_filter:
                continue
            if not isinstance(y_full, pd.Series) or y_full.empty or len(y_full) <= test_size:
                continue
            block = forecast_metrics.get(metric, {})
            if not isinstance(block, dict):
                continue
            y_train, y_test = y_full.iloc[:-test_size], y_full.iloc[-test_size:]
            y_train = _filter_series_time_range(y_train, start_ms=start_ms, end_ms=end_ms)
            if history_points is not None:
                points = max(0, int(history_points))
                y_train = y_train.iloc[-points:] if points else y_train.iloc[0:0]
            metric_out[str(metric)] = {
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
        if metric_out:
            out[str(container)] = metric_out
    return out


def _filter_series_time_range(
    series: pd.Series,
    *,
    start_ms: Optional[int],
    end_ms: Optional[int],
) -> pd.Series:
    result = series
    if start_ms is not None:
        result = result[result.index >= pd.Timestamp(int(start_ms), unit="ms")]
    if end_ms is not None:
        result = result[result.index <= pd.Timestamp(int(end_ms), unit="ms")]
    return result

