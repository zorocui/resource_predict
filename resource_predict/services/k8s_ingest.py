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
from resource_predict.providers.k8s_prometheus import k8s_workload_prometheus_provider
from resource_predict.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# K8S Prometheus 后台定时调度器
# ---------------------------------------------------------------------------
_k8s_stop_event = threading.Event()
_k8s_scheduler_thread: Optional[threading.Thread] = None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_k8s_prometheus_items(
    clusters: Optional[Iterable[str]] = None,
    *,
    history_hours: Optional[float] = None,
) -> List[Dict[str, Any]]:
    items = k8s_workload_prometheus_provider(
        resources=0,
        n=0,
        freq="5min",
        clusters=clusters,
        history_hours=history_hours,
    )
    if not isinstance(items, list) or not items:
        raise RuntimeError("Prometheus provider returned no K8S workload resources")
    return items


def run_k8s_prometheus_upsert(
    *,
    clusters: Optional[Iterable[str]] = None,
    fail_if_busy: bool = False,
    full_refresh: bool = False,
    trigger_source: str = "manual",
) -> Dict[str, Any]:
    """Fetch K8S Workload metrics from Prometheus and merge them into outputs."""
    cluster_list = list(clusters) if clusters is not None else None
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
        items = fetch_k8s_prometheus_items(cluster_list, history_hours=history_hours)
        fetch_finished_at = _utc_timestamp()
        logger.info(
            "[k8s_ingest] K8S Prometheus fetch finished: resources=%d elapsed=%.2fs "
            "started_at=%s finished_at=%s",
            len(items),
            time.perf_counter() - fetch_started_perf,
            fetch_started_at,
            fetch_finished_at,
        )

        result = run_upsert_with_data(items, fail_if_busy=fail_if_busy, out_dir=out_dir)
        if not result.get("success"):
            mark_external_update_failed(str(result.get("error") or "K8S Prometheus 数据拉取失败"))
        else:
            mark_external_update_finished(result)
        return result
    except Exception as exc:
        mark_external_update_failed(str(exc))
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
        try:
            run_k8s_prometheus_upsert(
                fail_if_busy=False,
                trigger_source="scheduled_startup" if first_run else "scheduled",
            )
            first_run = False
        except Exception as exc:
            logger.error("[k8s_ingest] 调度循环异常: %s", exc)

        _k8s_stop_event.wait(interval_seconds)

    logger.info("[k8s_ingest] K8S Prometheus 后台调度器已停止")


def start_k8s_background_updater(
    interval_minutes: Optional[int] = None,
) -> Optional[threading.Thread]:
    """
    启动 K8S Prometheus 后台定时拉取线程。

    参数可选，未传入时从 settings.k8s_prometheus 读取默认值。
    若 scheduled_update_enabled 为 False，返回 None。
    """
    global _k8s_scheduler_thread

    cfg = settings.k8s_prometheus
    interval = (
        interval_minutes
        if interval_minutes is not None
        else int(cfg.scheduled_update_interval_minutes)
    )
    startup_delay = max(0, int(cfg.scheduled_update_startup_delay_seconds))

    if not cfg.scheduled_update_enabled:
        logger.info(
            "[k8s_ingest] 配置中 scheduled_update_enabled=False，跳过 K8S 后台定时拉取"
            "（可通过 POST /api/cluster-configs/k8s-fetch 手动触发）"
        )
        return None

    # 如果已有线程在跑，先停掉旧的
    if _k8s_scheduler_thread is not None and _k8s_scheduler_thread.is_alive():
        logger.warning("[k8s_ingest] 检测到已有 K8S 调度线程在运行，先停止旧线程")
        stop_k8s_background_updater()

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


def stop_k8s_background_updater(timeout: float = 10.0) -> None:
    """通知 K8S 后台线程停止并等待其退出。"""
    global _k8s_scheduler_thread
    _k8s_stop_event.set()
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
