from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from resource_predict.data.updater import (
    mark_external_update_failed,
    mark_external_update_finished,
    mark_external_update_started,
    run_upsert_with_data,
)
from resource_predict.data.raw_store import RawResourceStore
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


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_k8s_prometheus_result(
    clusters: Optional[Iterable[str]] = None,
    *,
    history_hours: Optional[float] = None,
) -> Dict[str, Any]:
    result = fetch_k8s_workload_prometheus_result(
        resources=0,
        n=0,
        freq="5min",
        clusters=clusters,
        history_hours=history_hours,
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
    try:
        out_dir = scoped_out_dir("k8s", settings.app.out_dir)
        history_hours = _history_hours_for_fetch(
            out_dir=out_dir,
            clusters=cluster_list,
            full_refresh=full_refresh,
        )
        window_label = _fetch_window_label(history_hours)
        source_label = _trigger_source_label(trigger_source)
        mark_external_update_started(
            "fetching_k8s_prometheus",
            f"{source_label}：正在从 K8S Prometheus 拉取 Workload 指标（{window_label}）",
            metadata={
                "task_source": source_label,
                "fetch_window_label": window_label,
            },
        )
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
        logger.info(
            "[k8s_ingest] K8S Prometheus fetch finished: resources=%d elapsed=%.2fs "
            "started_at=%s finished_at=%s",
            len(items),
            time.perf_counter() - fetch_started_perf,
            fetch_started_at,
            fetch_finished_at,
        )

        step_seconds = max(
            1,
            int(getattr(settings.k8s_prometheus, "step_seconds", 300)),
        )
        result = dict(
            run_upsert_with_data(
                items,
                fail_if_busy=fail_if_busy,
                out_dir=out_dir,
                freq_hint=f"{step_seconds}s",
            )
        )
        result["cluster_results"] = cluster_results
        if not result.get("success"):
            result["status"] = "failed"
            mark_external_update_failed(
                str(result.get("error") or "K8S Prometheus 数据拉取失败"),
                cluster_results=cluster_results,
            )
        else:
            result["status"] = cluster_status
            mark_external_update_finished(result)
        return result
    except Exception as exc:
        mark_external_update_failed(str(exc), cluster_results=cluster_results)
        raise


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
    if float(history_hours).is_integer():
        return f"增量窗口：最近 {int(history_hours)} 小时"
    return f"增量窗口：最近 {float(history_hours):.1f} 小时"


def _trigger_source_label(trigger_source: str) -> str:
    if trigger_source == "scheduled_startup":
        return "K8S 后台定时拉取（启动后首次拉取）"
    if trigger_source == "scheduled":
        return "K8S 后台定时拉取"
    return "页面手动拉取"


def _has_existing_k8s_raw_data(out_dir: Path, clusters: Optional[Iterable[str]]) -> bool:
    generation_cfg = getattr(settings, "generation", None)
    store = RawResourceStore(
        out_dir,
        max_cache_items=int(getattr(generation_cfg, "raw_resource_cache_items", 100)),
    )
    if not store.exists():
        return False
    try:
        resource_ids = store.resource_ids()
    except Exception:
        return False
    if not resource_ids:
        return False
    wanted = {str(x).strip() for x in clusters or [] if str(x).strip()}
    if not wanted:
        return True
    return any(
        len(parts := resource_id.split(":")) >= 2 and parts[0] == "k8s" and parts[1] in wanted
        for resource_id in resource_ids
    )


# ---------------------------------------------------------------------------
# K8S Prometheus 后台定时调度器生命周期
# ---------------------------------------------------------------------------


def _k8s_scheduler_loop(interval_seconds: float, startup_delay_seconds: float) -> None:
    """后台线程主循环：按间隔定时触发 K8S Prometheus 数据拉取 + upsert。"""
    logger.info(
        "[k8s_ingest] K8S Prometheus 后台调度器已启动，间隔 %.0f 秒（%.0f 分钟）",
        interval_seconds,
        interval_seconds / 60.0,
    )
    if startup_delay_seconds > 0:
        logger.info("[k8s_ingest] 首次自动拉取将在 %.0f 秒后执行", startup_delay_seconds)
        if _k8s_stop_event.wait(startup_delay_seconds):
            logger.info("[k8s_ingest] 后台调度器在首次拉取前停止")
            return
    first_run = True
    while not _k8s_stop_event.is_set():
        cfg = settings.k8s_prometheus
        if not cfg.scheduled_update_enabled:
            logger.info("[k8s_ingest] K8S 定时拉取已关闭，等待配置变更")
            _k8s_reload_event.wait()
            _k8s_reload_event.clear()
            continue
        try:
            run_k8s_prometheus_upsert(
                fail_if_busy=False,
                trigger_source="scheduled_startup" if first_run else "scheduled",
            )
            first_run = False
        except Exception as exc:
            logger.error("[k8s_ingest] 调度循环异常: %s", exc)

        interval_seconds = max(60.0, float(cfg.scheduled_update_interval_minutes) * 60.0)
        _k8s_reload_event.wait(interval_seconds)
        _k8s_reload_event.clear()

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
    startup_delay = max(0, int(cfg.scheduled_update_startup_delay_seconds))

    if _k8s_scheduler_thread is not None and _k8s_scheduler_thread.is_alive():
        return _k8s_scheduler_thread

    _k8s_stop_event.clear()
    _k8s_scheduler_thread = threading.Thread(
        target=_k8s_scheduler_loop,
        args=(interval * 60.0, float(startup_delay)),
        daemon=True,
        name="k8s-updater",
    )
    _k8s_scheduler_thread.start()
    logger.info("[k8s_ingest] K8S 后台线程已启动（daemon=True）")
    return _k8s_scheduler_thread


def notify_k8s_scheduler_config_changed() -> None:
    """唤醒唯一调度线程，使其在控制边界读取最新运行配置。"""
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
