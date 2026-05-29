from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from resource_predict.data.updater import (
    mark_external_update_failed,
    mark_external_update_finished,
    mark_external_update_started,
    run_upsert_with_data,
)
from resource_predict.pipeline.output_paths import scoped_out_dir
from resource_predict.providers.k8s_prometheus import k8s_workload_prometheus_provider
from resource_predict.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# K8S Prometheus 后台定时调度器
# ---------------------------------------------------------------------------
_k8s_stop_event = threading.Event()
_k8s_scheduler_thread: Optional[threading.Thread] = None


def fetch_k8s_prometheus_items(clusters: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    items = k8s_workload_prometheus_provider(resources=0, n=0, freq="5min", clusters=clusters)
    if not isinstance(items, list) or not items:
        raise RuntimeError("Prometheus provider returned no K8S workload resources")
    return items


def run_k8s_prometheus_upsert(
    *,
    clusters: Optional[Iterable[str]] = None,
    fail_if_busy: bool = False,
) -> Dict[str, Any]:
    """Fetch K8S Workload metrics from Prometheus and merge them into outputs."""
    mark_external_update_started("fetching_k8s_prometheus", "正在从 K8S Prometheus 拉取 Workload 指标")
    try:
        items = fetch_k8s_prometheus_items(clusters)
        out_dir = scoped_out_dir("k8s", settings.app.out_dir)

        result = run_upsert_with_data(items, fail_if_busy=fail_if_busy, out_dir=out_dir)
        if not result.get("success"):
            mark_external_update_failed(str(result.get("error") or "K8S Prometheus 数据拉取失败"))
        else:
            mark_external_update_finished(result)
        return result
    except Exception as exc:
        mark_external_update_failed(str(exc))
        raise


# ---------------------------------------------------------------------------
# K8S Prometheus 后台定时调度器生命周期
# ---------------------------------------------------------------------------


def _k8s_scheduler_loop(interval_seconds: float) -> None:
    """后台线程主循环：按间隔定时触发 K8S Prometheus 数据拉取 + upsert。"""
    logger.info(
        "[k8s_ingest] K8S Prometheus 后台调度器已启动，间隔 %.0f 秒（%.0f 分钟）",
        interval_seconds,
        interval_seconds / 60.0,
    )
    while not _k8s_stop_event.is_set():
        try:
            run_k8s_prometheus_upsert(fail_if_busy=False)
        except Exception as exc:
            logger.error("[k8s_ingest] 调度循环异常: %s", exc)

        # 分段等待，以便及时响应停止信号
        waited = 0.0
        while waited < interval_seconds and not _k8s_stop_event.is_set():
            time.sleep(1.0)
            waited += 1.0

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
        args=(interval * 60.0,),
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
