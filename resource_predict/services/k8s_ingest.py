from __future__ import annotations

import logging
import json
import math
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from resource_predict.data.updater import (
    _update_exclusive,
    UpdateBusyError,
    mark_external_update_failed,
    mark_external_update_finished,
    mark_external_update_started,
    run_upsert_with_data,
)
from resource_predict.data.raw_store import RawResourceStore
from resource_predict.data.io import atomic_write_json
from resource_predict.services.update_history import get_update_history, UPDATE_HISTORY_RETENTION
from resource_predict.pipeline.output_paths import scoped_out_dir
from resource_predict.providers.k8s_prometheus import fetch_k8s_workload_prometheus_result
from resource_predict.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# K8S Prometheus 后台定时调度器
# ---------------------------------------------------------------------------
_k8s_stop_event = threading.Event()
_k8s_reload_event = threading.Event()
_k8s_scheduler_thread: Optional[threading.Thread] = None


def _load_scheduler_anchor() -> float:
    """重启恢复自动调度基准；旧部署从自动拉取历史迁移，忽略手动任务。"""
    now = time.time()
    path = Path(settings.app.out_dir) / "k8s_scheduler_state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        anchor = payload.get("anchor_epoch_seconds")
        if payload.get("version") == 1 and type(anchor) in (int, float) and math.isfinite(anchor) and 0 < anchor <= now:
            return float(anchor)
        logger.warning("[k8s_ingest] 调度状态无效，尝试从自动拉取历史恢复: %s", path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        logger.exception("[k8s_ingest] 无法读取调度状态，尝试历史恢复: %s", path)
    sources = {_trigger_source_label("scheduled"), _trigger_source_label("scheduled_startup")}
    anchors = [record.get("started_at") for record in get_update_history(
        limit=UPDATE_HISTORY_RETENTION, out_dir=settings.app.out_dir) if record.get("task_source") in sources]
    valid = [float(value) for value in anchors
             if type(value) in (int, float) and math.isfinite(value) and 0 < value <= now]
    return max(valid) if valid else now


def _save_scheduler_anchor(anchor: float) -> None:
    try:
        atomic_write_json(Path(settings.app.out_dir) / "k8s_scheduler_state.json",
                          {"version": 1, "anchor_epoch_seconds": anchor})
    except OSError:
        logger.exception("[k8s_ingest] 无法保存调度时间，当前进程继续按原周期运行")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_k8s_prometheus_result(
    clusters: Optional[Iterable[str]] = None,
    *,
    history_hours: Optional[float] = None,
) -> Dict[str, Any]:
    # 批量/定时拉取中，新集群不能继承其他集群的增量窗口。
    windows = None
    if history_hours is not None:
        windows = {cluster: history_hours for cluster in _existing_k8s_raw_clusters(
            scoped_out_dir("k8s", settings.app.out_dir))}
    result = fetch_k8s_workload_prometheus_result(
        resources=0,
        n=0,
        freq="5min",
        clusters=clusters,
        history_hours=history_hours,
        history_hours_by_cluster=windows,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Prometheus provider returned an invalid K8S fetch result")
    return result


def _cluster_terminal_status(cluster_results: List[Dict[str, Any]]) -> str:
    succeeded = sum(1 for item in cluster_results if item.get("status") == "success")
    failed = sum(1 for item in cluster_results if item.get("status") == "failed")
    if succeeded and failed:
        return "partial_success"
    if succeeded:
        return "success"
    return "failed"


def run_k8s_prometheus_upsert(
    *,
    clusters: Optional[Iterable[str]] = None,
    fail_if_busy: bool = False,
    full_refresh: bool = False,
    trigger_source: str = "manual",
) -> Dict[str, Any]:
    """Fetch K8S Workload metrics from Prometheus and merge them into outputs."""
    cluster_list = list(clusters) if clusters is not None else None
    cluster_results: List[Dict[str, Any]] = []
    # Own the complete fetch/update/history lifecycle before touching shared status.
    if not _update_exclusive.acquire(blocking=not fail_if_busy):
        raise UpdateBusyError("已有更新任务正在运行中")
    started = False
    source_label = _trigger_source_label(trigger_source)
    try:
        out_dir = scoped_out_dir("k8s", settings.app.out_dir)
        history_hours = _history_hours_for_fetch(
            out_dir=out_dir,
            clusters=cluster_list,
            full_refresh=full_refresh,
        )
        window_label = _fetch_window_label(history_hours)
        mark_external_update_started(
            "fetching_k8s_prometheus",
            f"{source_label}：正在从 K8S Prometheus 拉取 Workload 指标（{window_label}）",
            metadata={
                "task_source": source_label,
                "fetch_window_label": window_label,
            },
        )
        started = True
        fetch_started_at = _utc_timestamp()
        fetch_started_perf = time.perf_counter()
        logger.info(
            "[k8s_ingest] K8S Prometheus fetch started: clusters=%s history_hours=%s "
            "full_refresh=%s started_at=%s",
            ",".join(str(x) for x in cluster_list) if cluster_list else "all",
            history_hours if history_hours is not None else "default",
            full_refresh,
            fetch_started_at,
        )
        fetch_result = fetch_k8s_prometheus_result(cluster_list, history_hours=history_hours)
        items = list(fetch_result.get("items") or [])
        cluster_results = [
            dict(item)
            for item in fetch_result.get("cluster_results") or []
            if isinstance(item, dict)
        ]
        cluster_status = _cluster_terminal_status(cluster_results)
        if not items:
            errors = [str(item.get("error")) for item in cluster_results if item.get("error")]
            detail = "；".join(errors) or "Prometheus provider returned no K8S workload resources"
            raise RuntimeError(f"所有 K8S Prometheus 集群拉取失败: {detail}")
        fetch_finished_at = _utc_timestamp()
        fetch_elapsed = time.perf_counter() - fetch_started_perf
        logger.info(
            "[k8s_ingest] K8S Prometheus fetch finished: resources=%d elapsed=%.2fs "
            "started_at=%s finished_at=%s",
            len(items),
            fetch_elapsed,
            fetch_started_at,
            fetch_finished_at,
        )

        step_seconds = max(
            1,
            int(getattr(settings.k8s_prometheus, "step_seconds", 600)),
        )
        result = dict(
            run_upsert_with_data(
                items,
                fail_if_busy=fail_if_busy,
                out_dir=out_dir,
                freq_hint=f"{step_seconds}s",
                task_source=source_label,
                _exclusive_already_acquired=True,
            )
        )
        result["cluster_results"] = cluster_results
        result["fetch_elapsed_seconds"] = round(fetch_elapsed, 2)
        result["upsert_elapsed_seconds"] = result.get("elapsed_seconds")
        result["elapsed_seconds"] = round(time.perf_counter() - fetch_started_perf, 2)
        if not result.get("success"):
            result["status"] = "failed"
            mark_external_update_failed(
                str(result.get("error") or "K8S Prometheus 数据拉取失败"),
                cluster_results=cluster_results,
            )
        else:
            result["status"] = "partial_success" if result.get("status") == "partial_success" else cluster_status
            mark_external_update_finished(result)
        return result
    except Exception as exc:
        if not started:
            mark_external_update_started(
                "fetching_k8s_prometheus", source_label,
                metadata={"task_source": source_label},
            )
        mark_external_update_failed(str(exc), cluster_results=cluster_results)
        raise
    finally:
        _update_exclusive.release()


def _history_hours_for_fetch(
    *,
    out_dir: Path,
    clusters: Optional[Iterable[str]],
    full_refresh: bool,
) -> Optional[float]:
    if full_refresh or not _has_existing_k8s_raw_data(out_dir, clusters):
        return None
    cfg = settings.k8s_prometheus
    minutes = int(getattr(cfg, "scheduled_update_interval_minutes", 360)) + int(
        getattr(cfg, "incremental_overlap_minutes", 60)
    )
    return max(1.0, minutes / 60.0)


def _fetch_window_label(history_hours: Optional[float]) -> str:
    if history_hours is None:
        days = int(getattr(settings.k8s_prometheus, "history_days", 7))
        return f"全量历史窗口：最近 {days} 天"
    hours = f"{float(history_hours):g}"
    return f"已有集群增量窗口：最近 {hours} 小时；无本地历史的集群使用全量历史窗口"


def _trigger_source_label(trigger_source: str) -> str:
    if trigger_source == "scheduled_startup":
        return "K8S 后台定时拉取（启动后首次拉取）"
    if trigger_source == "scheduled":
        return "K8S 后台定时拉取"
    return "页面手动拉取"


def _has_existing_k8s_raw_data(out_dir: Path, clusters: Optional[Iterable[str]]) -> bool:
    existing = _existing_k8s_raw_clusters(out_dir)
    wanted = {str(x).strip() for x in clusters or [] if str(x).strip()}
    return bool(existing & wanted if wanted else existing)


def _existing_k8s_raw_clusters(out_dir: Path) -> set[str]:
    generation_cfg = getattr(settings, "generation", None)
    store = RawResourceStore(
        out_dir,
        max_cache_items=int(getattr(generation_cfg, "raw_resource_cache_items", 100)),
    )
    if not store.exists():
        return set()
    try:
        resource_ids = store.resource_ids()
    except Exception:
        return set()
    return {
        parts[1]
        for resource_id in resource_ids
        if len(parts := resource_id.split(":")) >= 2 and parts[0] == "k8s" and parts[1]
    }


# ---------------------------------------------------------------------------
# K8S Prometheus 后台定时调度器生命周期
# ---------------------------------------------------------------------------


def _k8s_scheduler_loop(interval_seconds: float) -> None:
    """后台线程主循环：按间隔定时触发 K8S Prometheus 数据拉取 + upsert。

    配置保存只唤醒本循环重读运行配置。拉取的唯一判定条件是到期时刻已经过去，到期时刻为
    ``last_start + max(60 秒, scheduled_update_interval_minutes)``，其中 ``last_start`` 是上一次
    拉取**开始**时的 monotonic 时刻（拉取异常同样占用本轮，因此失败后等满一个周期而不是快速
    重试），重启从持久化时间恢复，首次接入才以启动时刻为基准。被提前唤醒但尚未到点时继续等待剩余时间，
    因此周期既不会被重置也不会被提前；只有重算后已逾期才会立即拉取，例如把周期改短到已过期，
    或关闭超过一个周期后重新打开。

    锚定开始时刻而不是完成时刻，是为了让两轮拉取的实际间隔严格等于配置周期：增量回看窗口固定
    为 ``scheduled_update_interval_minutes + incremental_overlap_minutes``，若按完成时刻计时，
    实际间隔会变成“周期 + 拉取耗时”，耗时超过 overlap 就会在窗口外留下永久取不到的时间段。
    """
    logger.info(
        "[k8s_ingest] K8S Prometheus 后台调度器已启动，间隔 %.0f 秒（%.0f 分钟）",
        interval_seconds,
        interval_seconds / 60.0,
    )
    anchor = _load_scheduler_anchor()
    _save_scheduler_anchor(anchor)
    # 跨进程保存墙上时间，进程内仍用单调时钟，避免校时扰动等待周期。
    last_start = time.monotonic() - max(0.0, time.time() - anchor)
    while not _k8s_stop_event.is_set():
        cfg = settings.k8s_prometheus
        if not cfg.scheduled_update_enabled:
            logger.info("[k8s_ingest] K8S 定时拉取已关闭，等待配置变更")
            _k8s_reload_event.wait()
            _k8s_reload_event.clear()
            continue

        # 每轮都按最新间隔重新推导到期时刻，因此改周期无需重启即可生效。
        interval_seconds = max(60.0, float(cfg.scheduled_update_interval_minutes) * 60.0)
        next_due = last_start + interval_seconds
        remaining = next_due - time.monotonic()
        if remaining > 0:
            _k8s_reload_event.wait(remaining)
            _k8s_reload_event.clear()
            if _k8s_stop_event.is_set():
                break
            if next_due - time.monotonic() > 0:
                # 被配置变更提前唤醒：重读配置后继续等待，不额外拉取。
                continue

        # 先记开始时刻再拉取：拉取耗时不会把下一轮往后推。
        last_start = time.monotonic()
        _save_scheduler_anchor(time.time())
        try:
            run_k8s_prometheus_upsert(
                fail_if_busy=False,
                trigger_source="scheduled",
            )
        except Exception as exc:
            logger.error("[k8s_ingest] 调度循环异常: %s", exc)
        elapsed = time.monotonic() - last_start
        if elapsed > interval_seconds:
            logger.warning(
                "[k8s_ingest] 本轮拉取耗时 %.0f 秒，已超过配置周期 %.0f 秒；下一轮会立即开始，"
                "且固定回看窗口（周期 + %d 分钟 overlap）可能盖不住实际间隔，"
                "请考虑拉长 scheduled_update_interval_minutes 或优化拉取耗时",
                elapsed,
                interval_seconds,
                int(getattr(cfg, "incremental_overlap_minutes", 60)),
            )

    logger.info("[k8s_ingest] K8S Prometheus 后台调度器已停止")


def start_k8s_background_updater(
    interval_minutes: Optional[int] = None,
) -> Optional[threading.Thread]:
    """
    启动 K8S Prometheus 后台定时拉取线程。

    参数可选，未传入时从 settings.k8s_prometheus 读取默认值。
    即使定时拉取关闭也保留一个等待配置变更的控制线程。
    """
    global _k8s_scheduler_thread

    cfg = settings.k8s_prometheus
    interval = (
        interval_minutes
        if interval_minutes is not None
        else int(cfg.scheduled_update_interval_minutes)
    )

    if _k8s_scheduler_thread is not None and _k8s_scheduler_thread.is_alive():
        return _k8s_scheduler_thread

    _k8s_stop_event.clear()
    _k8s_scheduler_thread = threading.Thread(
        target=_k8s_scheduler_loop,
        args=(interval * 60.0,),
        daemon=True,
        name="k8s-updater",
    )
    _k8s_scheduler_thread.start()
    logger.info("[k8s_ingest] K8S 后台线程已启动（daemon=True）")
    return _k8s_scheduler_thread


def notify_k8s_scheduler_config_changed() -> None:
    """唤醒唯一调度线程，使其在控制边界读取最新运行配置。

    仅触发配置重读，不会额外引发一轮拉取；下一次拉取仍按原定周期发生。
    """
    _k8s_reload_event.set()


def stop_k8s_background_updater(timeout: float = 10.0) -> None:
    """通知 K8S 后台线程停止并等待其退出。"""
    global _k8s_scheduler_thread
    _k8s_stop_event.set()
    _k8s_reload_event.set()
    if _k8s_scheduler_thread is not None and _k8s_scheduler_thread.is_alive():
        logger.info("[k8s_ingest] 等待 K8S 后台调度线程退出 …")
        _k8s_scheduler_thread.join(timeout=timeout)
        if _k8s_scheduler_thread.is_alive():
            logger.warning(
                "[k8s_ingest] K8S 后台调度线程未在 %.0fs 内退出", timeout
            )
        else:
            logger.info("[k8s_ingest] K8S 后台调度线程已退出")
    _k8s_scheduler_thread = None
