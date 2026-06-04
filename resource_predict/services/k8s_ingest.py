from __future__ import annotations

import json
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
) -> Dict[str, Any]:
    """Fetch K8S Workload metrics from Prometheus and merge them into outputs."""
    cluster_list = list(clusters) if clusters is not None else None
    mark_external_update_started("fetching_k8s_prometheus", "正在从 K8S Prometheus 拉取 Workload 指标")
    try:
        out_dir = scoped_out_dir("k8s", settings.app.out_dir)
        history_hours = _history_hours_for_fetch(
            out_dir=out_dir,
            clusters=cluster_list,
            full_refresh=full_refresh,
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


def _has_existing_k8s_raw_data(out_dir: Path, clusters: Optional[Iterable[str]]) -> bool:
    raw_path = out_dir / "raw_data.json"
    if not raw_path.exists():
        return False
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    resources = payload.get("resources", []) if isinstance(payload, dict) else []
    if not isinstance(resources, list) or not resources:
        return False
    wanted = {str(x).strip() for x in clusters or [] if str(x).strip()}
    if not wanted:
        return True
    for item in resources:
        if not isinstance(item, dict):
            continue
        spec = item.get("spec", {}) if isinstance(item.get("spec"), dict) else {}
        if str(spec.get("cluster") or "").strip() in wanted:
            return True
    return False


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
