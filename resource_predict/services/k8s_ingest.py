from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from resource_predict.data.updater import (
    mark_external_update_failed,
    mark_external_update_started,
    run_upsert_with_data,
)
from resource_predict.pipeline.output_paths import scoped_out_dir
from resource_predict.providers.k8s_prometheus import k8s_workload_prometheus_provider
from resource_predict.settings import settings


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
        return result
    except Exception as exc:
        mark_external_update_failed(str(exc))
        raise
