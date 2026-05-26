"""
定时/手动数据更新与 raw 文件合并。

功能：
- 按配置间隔（或 API）拉取/推送各资源最新监控数据并合并到 raw_data.json
- 默认全量保留历史；可选 sliding_window 在写盘前按净增量裁掉最旧数据
- 前端展示窗口由 display_window_points 控制（仅影响图表，不写回 raw）
- 合并完成后调用 generate_predictions_only() 重算预测
- 后台调度使用 daemon 线程

可替换数据源：
- 默认 resource_predict.providers.mock.mock_incremental_provider；或配置 incremental_provider_path，
  或使用 POST /api/update-data 推送
"""

from __future__ import annotations

import importlib
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pandas as pd
import numpy as np

from resource_predict.settings import settings
from resource_predict.data.io import coerce_metric_series, read_raw_dataset, write_raw_dataset
from resource_predict.pipeline.constants import RAW_DATA_FILENAME
from resource_predict.pipeline.output_paths import scoped_out_dir, split_items_by_scope
from resource_predict.providers.mock import mock_incremental_provider
from resource_predict.resource_types import metric_names_for_resource

logger = logging.getLogger(__name__)


def backup_raw_dataset(raw_path: Path) -> Optional[Path]:
    """Back up raw_data.json before an in-place merge/write."""
    if not raw_path.exists():
        return None
    backup_dir = raw_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{raw_path.stem}.{stamp}.json"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"{raw_path.stem}.{stamp}-{suffix}.json"
        suffix += 1
    shutil.copy2(raw_path, backup_path)
    return backup_path

# ---------------------------------------------------------------------------
# 可插拔增量数据源接口
# ---------------------------------------------------------------------------
# 函数签名为：
#   (prepared_resources: List[Dict], points_to_add: int) -> List[Dict]
# 其中 prepared_resources 结构与 read_raw_dataset 返回一致：
#   [{"resource_id": str, "spec": {...}, "cpu": Series, "memory": Series, "disk": Series}, ...]
# 返回值结构需与 data_provider 格式一致：
#   [{"resource_id": str, "metrics": {"cpu": {"timestamps":[], "values":[]}, ...}}, ...]
IncrementalProvider = Callable[
    [List[Dict[str, Any]], int],
    List[Dict[str, Any]],
]

# ---------------------------------------------------------------------------
# 增量数据标准格式（用于 POST /api/update-data 推送）
# ---------------------------------------------------------------------------
# 请求体为 JSON 数组，每项表示一个资源的增量数据：
# [
#   {
#     "resource_id": "vm-001",
#     "metrics": {
#       "cpu":    {"timestamps": [1778500000000, ...], "values": [0.45, ...]},
#       "memory": {"timestamps": [1778500000000, ...], "values": [0.72, ...]},
#       "disk":   {"timestamps": [1778500000000, ...], "values": [0.33, ...]}
#     }
#   },
#   ...
# ]
# - timestamps：毫秒级 Unix 时间戳
# - values：使用率小数 [0, 1]（非百分比）
# - 每个指标数组长度可不等（0 表示该指标无新数据，将跳过）
# - 若 timestamps 与 values 均非空，二者长度必须一致


class UpdateBusyError(RuntimeError):
    """已有数据更新流程正在执行（非阻塞模式下无法获取排他锁）。"""


def _resolve_provider() -> IncrementalProvider:
    """根据配置解析增量数据源：自定义路径 > mock 兜底。"""
    path = (settings.update.incremental_provider_path or "").strip()
    if not path:
        return mock_incremental_provider

    if ":" not in path:
        raise ValueError(
            f"incremental_provider_path 格式错误，应为 'module:function'，实际为: {path!r}"
        )
    module_path, func_name = path.split(":", 1)
    module_path = module_path.strip()
    func_name = func_name.strip()
    if not module_path or not func_name:
        raise ValueError(
            f"incremental_provider_path 格式错误，应为 'module:function'，实际为: {path!r}"
        )

    try:
        mod = importlib.import_module(module_path)
    except Exception as exc:
        raise ImportError(
            f"无法导入增量数据源模块 '{module_path}'：{exc}"
        ) from exc

    func = getattr(mod, func_name, None)
    if func is None:
        raise AttributeError(
            f"模块 '{module_path}' 中未找到函数 '{func_name}'"
        )
    logger.info("[updater] 已加载自定义增量数据源: %s", path)
    return func  # type: ignore[return-value]

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
_update_status: Dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "current_resource_ids": [],
    "current_metrics_by_resource": {},
    "resources_updated": 0,
    "resources_created": 0,
    "created_resource_ids": [],
    "predicted_resources": 0,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_result": None,
    "total_updates": 0,
    "total_new_points": 0,
}

_stop_event = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
# 保护「读 raw → 合并 → 写 raw → 重预测」全过程，与 HTTP / 调度器互斥
_update_exclusive = threading.Lock()


def get_update_status() -> Dict[str, Any]:
    """返回当前更新状态（线程安全）。"""
    with _lock:
        return dict(_update_status)


def mark_external_update_started(phase: str, message: str = "") -> None:
    """Mark a non-standard update task, such as K8S Prometheus fetch, as running."""
    now = time.time()
    with _lock:
        _update_status["running"] = True
        _update_status["phase"] = phase
        _update_status["last_started_at"] = now
        _update_status["last_finished_at"] = None
        _update_status["last_error"] = None
        _update_status["last_result"] = None
        if message:
            _update_status["message"] = message


def mark_external_update_failed(error: str, phase: str = "error") -> None:
    with _lock:
        _update_status["running"] = False
        _update_status["phase"] = phase
        _update_status["last_error"] = error
        _update_status["last_finished_at"] = time.time()
        _update_status["message"] = error


def mark_external_update_finished(result: Dict[str, Any]) -> None:
    with _lock:
        _update_status["running"] = False
        _update_status["phase"] = "idle"
        _update_status["last_error"] = None
        _update_status["last_finished_at"] = time.time()
        _update_status["last_result"] = dict(result)
        _update_status["resources_updated"] = int(result.get("resources_updated") or 0)
        _update_status["resources_created"] = int(result.get("resources_created") or 0)
        _update_status["predicted_resources"] = int(result.get("predicted_resources") or 0)
        _update_status["created_resource_ids"] = list(result.get("created_resource_ids") or [])
        _update_status["total_updates"] += 1
        _update_status["total_new_points"] += int(result.get("total_new_points") or 0)
        _update_status["message"] = "K8S Prometheus 数据拉取完成"


def _normalize_one_timestamp_ms(t: Any) -> int:
    """将单个时间戳转为毫秒 Unix int。"""
    if isinstance(t, bool):
        raise TypeError("bool 不能作为时间戳")
    if isinstance(t, (int, float)):
        v = float(t)
        return int(v) if v >= 1e12 else int(v * 1000)
    if isinstance(t, str):
        idx = pd.to_datetime([t], errors="coerce")
        if pd.isna(idx[0]):
            raise ValueError(f"无法解析时间戳字符串: {t!r}")
        return int(idx.view("int64")[0] // 1_000_000)
    raise TypeError(
        f"不支持的 timestamp 元素类型: {type(t).__name__}，"
        f"应为 int/float（Unix）或 str"
    )


def _normalize_timestamps_ms(ts_list: List[Any]) -> List[int]:
    """
    将各元素独立规范为毫秒级 Unix 时间戳；一行内可混用秒/毫秒数字或 ISO 字符串。
    """
    if not ts_list:
        return []
    return [_normalize_one_timestamp_ms(t) for t in ts_list]


def _validate_incoming_data(
    existing_series: pd.Series,
    new_timestamps: List[Any],
    metric_name: str = "",
    resource_id: str = "",
) -> Dict[str, Any]:
    """
    校验新数据与现有数据的时间连续性，返回警告信息。

    检测项：
    - 重复时间点（与现有数据重叠）
    - 时间不连续（新旧数据间存在较大间隔）
    - 时间倒序（新数据早于现有数据末点）
    - 新数据内部乱序
    """
    warnings: List[str] = []
    tag = f"[{resource_id}][{metric_name}] " if resource_id and metric_name else ""

    if existing_series.empty or not new_timestamps:
        return {"ok": True, "warnings": warnings, "duplicate_count": 0}

    # 规范化并排序
    try:
        ts_ms = sorted(_normalize_timestamps_ms(new_timestamps))
    except Exception as exc:
        return {"ok": False, "warnings": [f"{tag}时间戳解析失败: {exc}"], "duplicate_count": 0}

    # 推断现有数据频率（用于判断间隔是否合理）
    existing_idx = existing_series.index
    try:
        freq = pd.infer_freq(existing_idx)
        expected_step_ms: Optional[int] = None
        if freq is not None:
            delta = pd.Timedelta(pd.tseries.frequencies.to_offset(freq))
            expected_step_ms = int(delta.total_seconds() * 1000)
    except Exception:
        expected_step_ms = None

    # 若无法推断频率，用中位数间隔代替
    if expected_step_ms is None and len(existing_idx) >= 3:
        diffs = np.diff(existing_idx.view("int64") // 1_000_000)
        diffs = diffs[diffs > 0]
        if diffs.size > 0:
            expected_step_ms = int(np.median(diffs))

    existing_ts_ms = (existing_idx.view("int64") // 1_000_000).tolist()

    # ----- 重复检测 ----
    existing_set = set(existing_ts_ms)
    duplicates = [t for t in ts_ms if t in existing_set]
    duplicate_count = len(duplicates)
    if duplicate_count > 0:
        warnings.append(
            f"{tag}{duplicate_count}/{len(ts_ms)} 个新数据点的时间戳与现有数据重复，"
            f"将被覆盖为最新值"
        )

    # ----- 新数据内乱序检测 -----
    raw_ts = _normalize_timestamps_ms(new_timestamps)
    if raw_ts != sorted(raw_ts):
        warnings.append(f"{tag}新数据的时间戳未按升序排列，已自动排序")

    # ----- 时间方向检测 -----
    last_existing_ms = max(existing_ts_ms)
    min_new_ms = min(ts_ms) if ts_ms else 0
    max_new_ms = max(ts_ms) if ts_ms else 0

    # 新数据全在历史数据之前/之中
    if max_new_ms <= last_existing_ms:
        warnings.append(
            f"{tag}新数据全部早于或等于现有数据末点，未引入新时间点"
            f"（新数据最大={max_new_ms}，现有末点={last_existing_ms}）"
        )
    # ----- 间隔过大检测 -----
    elif expected_step_ms is not None and expected_step_ms > 0:
        gap_ms = min_new_ms - last_existing_ms
        max_gap = max(expected_step_ms * 5, 3600_000 * 2)  # 至少容忍 2 小时
        if gap_ms > max_gap:
            gap_hours = gap_ms / 3600_000.0
            expected_hours = expected_step_ms / 3600_000.0
            warnings.append(
                f"{tag}新旧数据间存在较大时间间隔："
                f"现有末点 → 新数据首点 = {gap_hours:.1f}h，"
                f"但期望间隔约 {expected_hours:.1f}h"
            )

    return {
        "ok": True,
        "warnings": warnings,
        "duplicate_count": duplicate_count,
        "new_first_ms": min(ts_ms) if ts_ms else None,
        "new_last_ms": max(ts_ms) if ts_ms else None,
        "existing_last_ms": last_existing_ms,
    }


def _merge_incremental_into_series(
    series: pd.Series,
    new_timestamps: List[Any],
    new_values: List[float],
) -> pd.Series:
    """将新数据点合并进 Series（去重保留最新），不在此处理 sliding_window。"""
    if not new_timestamps or not new_values:
        return series

    if len(new_timestamps) != len(new_values):
        raise ValueError(
            f"timestamps 与 values 长度不一致：{len(new_timestamps)} vs {len(new_values)}"
        )

    # 自动适配多种时间戳格式 → 统一为毫秒 int
    ts_ms = _normalize_timestamps_ms(new_timestamps)

    # 构造新数据 Series
    new_idx = pd.to_datetime(ts_ms, unit="ms", errors="coerce")
    new_s = pd.Series(new_values, index=new_idx, dtype=float)
    new_s = new_s.dropna()

    if new_s.empty:
        return series

    combined = pd.concat([series, new_s])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined = combined.sort_index()

    return combined


def _build_new_resource_from_upsert(item: Dict[str, Any]) -> Dict[str, Any]:
    """Create a prepared resource from an upsert item that is not yet in raw_data.json."""
    rid = str(item.get("resource_id", "")).strip()
    if not rid:
        raise ValueError("新增资源缺少 resource_id")

    metrics = item.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError(f"{rid} 的 metrics 必须为 dict")

    prepared: Dict[str, Any] = {
        "resource_id": rid,
        "spec": item.get("spec", {}) if isinstance(item.get("spec"), dict) else {},
    }
    resource_type = str(item.get("resource_type") or "")
    if resource_type:
        prepared["resource_type"] = resource_type
    if isinstance(item.get("data_quality"), dict):
        prepared["data_quality"] = item["data_quality"]

    for metric in metric_names_for_resource(prepared):
        metric_data = metrics.get(metric)
        if not isinstance(metric_data, dict):
            raise ValueError(f"新增资源 {rid} 缺少 {metric} 指标数据")
        ts_list = metric_data.get("timestamps", [])
        val_list = metric_data.get("values", [])
        if not ts_list or not val_list:
            raise ValueError(f"新增资源 {rid} 的 {metric} 指标 timestamps/values 不能为空")
        if len(ts_list) != len(val_list):
            raise ValueError(
                f"新增资源 {rid} 的 {metric} timestamps 与 values 长度不一致："
                f"{len(ts_list)} vs {len(val_list)}"
            )
        prepared[metric] = coerce_metric_series(metric_data, metric)
    return prepared


def run_update_with_data(
    new_data_list: List[Dict[str, Any]],
    *,
    fail_if_busy: bool = False,
    out_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    使用外部传入的增量数据进行更新 + 预测重算（push 模式）。

    参数
    ----
    new_data_list : List[Dict]
        增量数据，格式见文件头注释中的 "增量数据标准格式"。
        与 IncrementalProvider 返回值一致：
        [{"resource_id": str, "metrics": {"cpu": {"timestamps":[], "values":[]}, ...}}, ...]

    fail_if_busy : bool
        为 True 时若已有更新在执行则抛出 UpdateBusyError（HTTP 层可映射为 409）。

    返回
    ----
    Dict : {"success": bool, "resources_updated": int, "total_new_points": int, ...}
    """
    logger.info("[updater] 收到 push 更新数据：%d 个资源", len(new_data_list) if isinstance(new_data_list, list) else 0)
    return _do_update(new_data_list=new_data_list, fail_if_busy=fail_if_busy, out_dir=out_dir)


def run_upsert_with_data(
    new_data_list: List[Dict[str, Any]],
    *,
    fail_if_busy: bool = False,
    out_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    使用外部传入数据进行显式 upsert：已有资源更新，不存在的资源插入。

    新增资源必须提供 cpu / memory / disk 三条完整非空序列；已有资源仍按增量格式
    合并，可只传部分指标。
    """
    logger.info(
        "[updater] 收到 push upsert 数据：%d 个资源",
        len(new_data_list) if isinstance(new_data_list, list) else 0,
    )
    return _do_update(
        new_data_list=new_data_list,
        fail_if_busy=fail_if_busy,
        allow_create=True,
        out_dir=out_dir,
    )


def run_scoped_update_with_data(
    new_data_list: List[Dict[str, Any]],
    *,
    fail_if_busy: bool = False,
) -> Dict[str, Any]:
    return _run_scoped_data_update(new_data_list, allow_create=False, fail_if_busy=fail_if_busy)


def run_scoped_upsert_with_data(
    new_data_list: List[Dict[str, Any]],
    *,
    fail_if_busy: bool = False,
) -> Dict[str, Any]:
    return _run_scoped_data_update(new_data_list, allow_create=True, fail_if_busy=fail_if_busy)


def run_update(
    *,
    incremental_provider: Optional[IncrementalProvider] = None,
    points_per_update: Optional[int] = None,
    fail_if_busy: bool = False,
) -> Dict[str, Any]:
    """
    执行一次完整的数据更新 + 预测重算流程（pull 模式，调用增量数据源拉取数据）。

    fail_if_busy : bool
        为 True 时若已有更新在执行则抛出 UpdateBusyError。

    返回本次更新的摘要 dict（含耗时、新增点数、错误信息等）。
    """
    cfg = settings.update
    points = points_per_update if points_per_update is not None else int(cfg.points_per_update)

    provider = incremental_provider or _resolve_provider()
    out_dir = scoped_out_dir("vm")
    raw_path = out_dir / RAW_DATA_FILENAME

    logger.info("[updater] 开始读取 raw_data.json …")
    prepared, meta = read_raw_dataset(raw_path)
    freq = str(meta.get("freq", settings.generation.freq))
    logger.info("[updater] 已读取 %d 个资源，freq=%s", len(prepared), freq)

    logger.info("[updater] 调用增量数据源，请求 %d 个新数据点 …", points)
    new_data_list = provider(prepared, points)

    return _do_update(
        new_data_list=new_data_list,
        prepared_cache=(prepared, meta, freq),
        fail_if_busy=fail_if_busy,
    )


# ---------------------------------------------------------------------------
# 内部核心：接收增量数据 → 可选滑动窗口 → 写回 → 重预测
# ---------------------------------------------------------------------------

def _do_update(
    *,
    new_data_list: List[Dict[str, Any]],
    prepared_cache: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any], str]] = None,
    fail_if_busy: bool = False,
    allow_create: bool = False,
    out_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    更新流程核心：run_update / run_update_with_data 复用。

    prepared_cache
        由 run_update(pull) 传入时已读取的 (prepared, meta, freq)，避免对 raw 二次读盘。
    fail_if_busy
        为 True 且无法立即取得排他锁时抛出 UpdateBusyError（由 API 映射为 409）。
    """
    output_dir = Path(out_dir) if out_dir is not None else Path(settings.app.out_dir)
    raw_path = output_dir / RAW_DATA_FILENAME

    result: Dict[str, Any] = {
        "success": False,
        "resources_updated": 0,
        "resources_created": 0,
        "total_new_points": 0,
        "elapsed_seconds": 0.0,
        "error": None,
        "warnings": [],
    }

    if not _update_exclusive.acquire(blocking=not fail_if_busy):
        raise UpdateBusyError("已有更新任务正在运行中")

    t_start = time.time()
    try:
        with _lock:
            _update_status["running"] = True
            _update_status["phase"] = "merging"
            _update_status["current_resource_ids"] = []
            _update_status["current_metrics_by_resource"] = {}
            _update_status["resources_updated"] = 0
            _update_status["resources_created"] = 0
            _update_status["created_resource_ids"] = []
            _update_status["predicted_resources"] = 0
            _update_status["last_started_at"] = t_start
            _update_status["last_error"] = None
            _update_status["last_result"] = None

        if not isinstance(new_data_list, list) or not new_data_list:
            raise ValueError("传入的增量数据为空")

        if prepared_cache is not None:
            prepared, _meta, freq = prepared_cache
            logger.info(
                "[updater] 使用与增量拉取一致的缓存：%d 个资源，freq=%s",
                len(prepared),
                freq,
            )
        else:
            logger.info("[updater] 开始读取 raw_data.json …")
            if allow_create and not raw_path.exists():
                prepared = []
                freq = settings.generation.freq
                logger.info("[updater] 目标 raw_data.json 不存在，将通过 upsert 初始化: %s", raw_path)
            else:
                prepared, meta = read_raw_dataset(raw_path)
                freq = str(meta.get("freq", settings.generation.freq))
            logger.info("[updater] 已读取 %d 个资源，freq=%s", len(prepared), freq)

        cfg_u = settings.update
        use_sw = bool(cfg_u.sliding_window)

        new_by_id: Dict[str, Dict[str, Any]] = {}
        for item in new_data_list:
            rid = str(item.get("resource_id", ""))
            if rid:
                new_by_id[rid] = item

        all_warnings: List[str] = []
        updated_count = 0
        created_count = 0
        total_new_pts = 0
        updated_resource_ids: List[str] = []
        created_resource_ids: List[str] = []
        updated_metrics_by_resource: Dict[str, List[str]] = {}

        for res in prepared:
            rid = str(res["resource_id"])
            new_info = new_by_id.get(rid)
            if new_info is None:
                continue

            spec_changed = False
            incoming_spec = new_info.get("spec", {})
            if isinstance(incoming_spec, dict) and incoming_spec:
                current_spec = res.get("spec", {})
                if not isinstance(current_spec, dict):
                    current_spec = {}
                merged_spec = {**current_spec, **incoming_spec}
                if merged_spec != current_spec:
                    res["spec"] = merged_spec
                    spec_changed = True
            incoming_type = str(new_info.get("resource_type") or "")
            if incoming_type:
                res["resource_type"] = incoming_type
                spec_changed = True
            if isinstance(new_info.get("data_quality"), dict):
                res["data_quality"] = new_info["data_quality"]
                spec_changed = True

            new_metrics = new_info.get("metrics", {})
            if not isinstance(new_metrics, dict):
                new_metrics = {}

            has_new = False
            changed_metrics: List[str] = []
            for metric in metric_names_for_resource(res):
                metric_new = new_metrics.get(metric, {})
                ts_list = metric_new.get("timestamps", []) if isinstance(metric_new, dict) else []
                val_list = metric_new.get("values", []) if isinstance(metric_new, dict) else []
                if not ts_list and not val_list:
                    continue
                if bool(ts_list) ^ bool(val_list):
                    raise ValueError(
                        f"{rid} 的 {metric}：timestamps 与 values 必须同时为空或同时非空"
                    )
                if ts_list and val_list:
                    validation = _validate_incoming_data(
                        res[metric],
                        ts_list,
                        metric_name=metric,
                        resource_id=rid,
                    )
                    if not validation.get("ok", True):
                        msg = "; ".join(validation.get("warnings") or ["时间戳校验未通过"])
                        raise ValueError(msg)
                    if validation.get("warnings"):
                        all_warnings.extend(validation["warnings"])

                    before_series = res[metric]
                    before_len = len(before_series)
                    updated_series = _merge_incremental_into_series(
                        before_series, ts_list, val_list,
                    )
                    if use_sw:
                        net = len(updated_series) - before_len
                        if net > 0:
                            updated_series = updated_series.iloc[net:]
                    res[metric] = updated_series
                    new_pts = max(0, len(res[metric]) - before_len)
                    metric_changed = not updated_series.equals(before_series)
                    total_new_pts += new_pts
                    if metric_changed:
                        has_new = True
                        changed_metrics.append(metric)

            if has_new or spec_changed:
                updated_count += 1
                updated_resource_ids.append(rid)
                if changed_metrics:
                    updated_metrics_by_resource[rid] = changed_metrics

        if allow_create:
            existing_ids = {str(res["resource_id"]) for res in prepared}
            for rid, new_info in new_by_id.items():
                if rid in existing_ids:
                    continue
                new_res = _build_new_resource_from_upsert(new_info)
                prepared.append(new_res)
                existing_ids.add(rid)
                created_count += 1
                created_resource_ids.append(rid)
                updated_resource_ids.append(rid)
                changed_metrics = list(metric_names_for_resource(new_res))
                updated_metrics_by_resource[rid] = changed_metrics
                total_new_pts += sum(len(new_res[metric]) for metric in changed_metrics)

        if all_warnings:
            for w in all_warnings:
                logger.warning("[updater] ⚠ %s", w)
        result["warnings"] = all_warnings

        if updated_count == 0 and created_count == 0:
            if allow_create:
                raise ValueError(
                    "没有任何资源被更新或插入（新数据与现有资源 ID 不匹配，或所有指标均为空）"
                )
            raise ValueError(
                "没有任何资源被更新（新数据与现有资源 ID 不匹配，或所有指标均为空）"
            )

        backup_path = backup_raw_dataset(raw_path)
        if backup_path is not None:
            logger.info("[updater] raw_data.json 备份: %s", backup_path)
        logger.info("[updater] 写回 raw_data.json （%d 个资源）…", len(prepared))
        with _lock:
            _update_status["phase"] = "writing_raw"
            _update_status["current_resource_ids"] = list(updated_resource_ids)
            _update_status["current_metrics_by_resource"] = dict(updated_metrics_by_resource)
            _update_status["resources_updated"] = updated_count
            _update_status["resources_created"] = created_count
            _update_status["created_resource_ids"] = list(created_resource_ids)
        write_raw_dataset(raw_path, prepared, freq=freq)

        logger.info("[updater] 开始重新预测 …")
        with _lock:
            _update_status["phase"] = "predicting"
        from resource_predict.pipeline import generate_predictions_only

        manifest = generate_predictions_only(
            out_dir=str(output_dir),
            resource_ids=updated_resource_ids,
            metric_names_by_resource=updated_metrics_by_resource,
        )
        logger.info("[updater] 预测完成，共 %d 个资源", len(manifest))

        elapsed = time.time() - t_start
        result["success"] = True
        result["resources_updated"] = updated_count
        result["resources_created"] = created_count
        result["elapsed_seconds"] = round(elapsed, 2)
        result["total_new_points"] = total_new_pts
        result["updated_resource_ids"] = updated_resource_ids
        result["created_resource_ids"] = created_resource_ids
        result["updated_metrics_by_resource"] = updated_metrics_by_resource
        result["predicted_resources"] = len(updated_resource_ids)

        with _lock:
            _update_status["last_finished_at"] = time.time()
            _update_status["last_error"] = None
            _update_status["phase"] = "idle"
            _update_status["predicted_resources"] = len(updated_resource_ids)
            _update_status["last_result"] = dict(result)
            _update_status["total_updates"] += 1
            _update_status["total_new_points"] += total_new_pts

        logger.info(
            "[updater] ✅ 更新完成：%d 更新，%d 新增，净增 %d 数据点，耗时 %.1fs",
            updated_count,
            created_count,
            total_new_pts,
            elapsed,
        )

    except Exception as exc:
        elapsed = time.time() - t_start
        result["error"] = str(exc)
        result["elapsed_seconds"] = round(elapsed, 2)
        logger.error("[updater] ❌ 更新失败 (%.1fs): %s", elapsed, exc)

        with _lock:
            _update_status["last_error"] = str(exc)
            _update_status["last_finished_at"] = time.time()
            _update_status["phase"] = "error"

    finally:
        with _lock:
            _update_status["running"] = False
            if _update_status.get("phase") != "error":
                _update_status["phase"] = "idle"
            _update_status["current_resource_ids"] = []
            _update_status["current_metrics_by_resource"] = {}
            _update_status["created_resource_ids"] = []
        _update_exclusive.release()

    return result


def _run_scoped_data_update(
    new_data_list: List[Dict[str, Any]],
    *,
    allow_create: bool,
    fail_if_busy: bool,
) -> Dict[str, Any]:
    split = split_items_by_scope(new_data_list)
    results: Dict[str, Any] = {
        "success": True,
        "scoped": True,
        "resources_updated": 0,
        "resources_created": 0,
        "total_new_points": 0,
        "predicted_resources": 0,
        "updated_resource_ids": [],
        "created_resource_ids": [],
        "updated_metrics_by_resource": {},
        "results_by_scope": {},
        "warnings": [],
        "error": None,
    }
    for scope, items in split.items():
        if not items:
            continue
        result = _do_update(
            new_data_list=items,
            fail_if_busy=fail_if_busy,
            allow_create=allow_create,
            out_dir=scoped_out_dir(scope),
        )
        results["results_by_scope"][scope] = result
        if not result.get("success"):
            results["success"] = False
            results["error"] = result.get("error")
            break
        results["resources_updated"] += int(result.get("resources_updated") or 0)
        results["resources_created"] += int(result.get("resources_created") or 0)
        results["total_new_points"] += int(result.get("total_new_points") or 0)
        results["predicted_resources"] += int(result.get("predicted_resources") or 0)
        results["updated_resource_ids"].extend(result.get("updated_resource_ids") or [])
        results["created_resource_ids"].extend(result.get("created_resource_ids") or [])
        results["warnings"].extend(result.get("warnings") or [])
        metrics_by_resource = result.get("updated_metrics_by_resource") or {}
        if isinstance(metrics_by_resource, dict):
            results["updated_metrics_by_resource"].update(metrics_by_resource)
    return results


# ---------------------------------------------------------------------------
# 后台调度器
# ---------------------------------------------------------------------------


def _scheduler_loop(
    interval_seconds: float,
    incremental_provider: Optional[IncrementalProvider],
    points_per_update: int,
) -> None:
    """后台线程主循环：按间隔定时触发 run_update。"""
    logger.info(
        "[updater] 后台调度器已启动，间隔 %.0f 秒（%.0f 分钟）",
        interval_seconds,
        interval_seconds / 60.0,
    )
    while not _stop_event.is_set():
        try:
            run_update(
                incremental_provider=incremental_provider,
                points_per_update=points_per_update,
            )
        except Exception as exc:
            logger.error("[updater] 调度循环异常: %s", exc)

        # 分段等待，以便及时响应停止信号
        waited = 0.0
        while waited < interval_seconds and not _stop_event.is_set():
            time.sleep(1.0)
            waited += 1.0

    logger.info("[updater] 后台调度器已停止")


def start_background_updater(
    interval_minutes: Optional[int] = None,
    *,
    incremental_provider: Optional[IncrementalProvider] = None,
    points_per_update: Optional[int] = None,
) -> Optional[threading.Thread]:
    """
    启动后台定时更新线程。

    参数均可选，未传入时从 config.settings.update 读取默认值。

    若 update.enabled 为 False，返回 None；否则返回已启动的 threading.Thread。
    """
    global _scheduler_thread

    cfg = settings.update
    interval = interval_minutes if interval_minutes is not None else int(cfg.interval_minutes)
    points = points_per_update if points_per_update is not None else int(cfg.points_per_update)

    if not bool(cfg.enabled):
        logger.info("[updater] 配置中 enabled=False，跳过后台自动更新（可通过 POST /api/update-trigger 手动触发）")
        return None

    # 如果已有线程在跑，先停掉旧的
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        logger.warning("[updater] 检测到已有调度线程在运行，先停止旧线程")
        stop_background_updater()

    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(interval * 60.0, incremental_provider, points),
        daemon=True,
        name="data-updater",
    )
    _scheduler_thread.start()
    logger.info("[updater] 后台线程已启动（daemon=True）")
    return _scheduler_thread


def stop_background_updater(timeout: float = 10.0) -> None:
    """通知后台线程停止并等待其退出。"""
    global _scheduler_thread
    _stop_event.set()
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        logger.info("[updater] 等待后台调度线程退出 …")
        _scheduler_thread.join(timeout=timeout)
        if _scheduler_thread.is_alive():
            logger.warning("[updater] 后台调度线程未在 %.0fs 内退出", timeout)
        else:
            logger.info("[updater] 后台调度线程已退出")
    _scheduler_thread = None
