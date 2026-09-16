from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import numpy as np
import pandas as pd

from resource_predict.settings import settings
from resource_predict.services.cluster_configs import (
    K8S_PROMETHEUS_CONFIG_PATH,
    ClusterConfigValidationError,
    read_k8s_prometheus_clusters,
)

logger = logging.getLogger(__name__)


class K8SWorkloadAggregationError(RuntimeError):
    """Prometheus 查询成功，但结果无法聚合出任何 K8S Workload。

    消息中带具体断点（容器序列 / owner 映射 / CPU 与内存配对），
    用于区分监控侧临时缺数据和配置或标签不匹配。
    """


# owner 映射或容器序列临时缺失时，整轮拉取的最大尝试次数（含首次）。
# kube-state-metrics 重启、Prometheus 查询限流等通常在数十秒内自愈，
# 因此按指数退避重试整轮，而不是把一次临时故障直接记为集群失败。
AGGREGATION_MAX_ATTEMPTS = 3
AGGREGATION_RETRY_BACKOFF_SECONDS = 15.0


def _is_retryable_http_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _retry_delay(base_seconds: float, attempt_index: int) -> float:
    return float(base_seconds) * (2 ** max(0, attempt_index))


def _merge_matrix_results(
    chunks: Iterable[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    samples: Dict[str, Dict[float, List[Any]]] = {}
    for chunk in chunks:
        for row in chunk:
            metric = row.get("metric", {})
            values = row.get("values", [])
            if not isinstance(metric, dict) or not isinstance(values, list):
                continue
            metric_key = json.dumps(metric, sort_keys=True, separators=(",", ":"))
            merged.setdefault(metric_key, {"metric": dict(metric), "values": []})
            metric_samples = samples.setdefault(metric_key, {})
            for sample in values:
                if not isinstance(sample, list) or len(sample) < 2:
                    continue
                try:
                    timestamp = float(sample[0])
                except (TypeError, ValueError):
                    continue
                metric_samples[timestamp] = sample

    out: List[Dict[str, Any]] = []
    for metric_key in sorted(merged):
        row = merged[metric_key]
        row["values"] = [samples[metric_key][ts] for ts in sorted(samples[metric_key])]
        out.append(row)
    return out


@dataclass(frozen=True)
class PrometheusClient:
    base_url: str
    bearer_token: str = ""
    basic_auth: str = ""
    timeout_seconds: int = 30
    max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    range_query_chunk_hours: int = 24

    def query(self, query: str, *, ts: Optional[float] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"query": query}
        if ts is not None:
            params["time"] = ts
        return self._get("/api/v1/query", params).get("result", [])

    def query_range(self, query: str, *, start: float, end: float, step: int) -> List[Dict[str, Any]]:
        chunk_seconds = max(1, int(self.range_query_chunk_hours)) * 3600
        if end <= start or end - start <= chunk_seconds:
            return self._get(
                "/api/v1/query_range",
                {"query": query, "start": start, "end": end, "step": step},
            ).get("result", [])

        chunks: List[List[Dict[str, Any]]] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(end, chunk_start + chunk_seconds)
            rows = self._get(
                "/api/v1/query_range",
                {"query": query, "start": chunk_start, "end": chunk_end, "step": step},
            ).get("result", [])
            chunks.append(rows)
            chunk_start = chunk_end
        return _merge_matrix_results(chunks)

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        if self.bearer_token:
            req.add_header("Authorization", f"Bearer {self.bearer_token}")
        if self.basic_auth:
            req.add_header("Authorization", f"Basic {self.basic_auth}")
        attempts = max(1, int(self.max_attempts))
        payload: Dict[str, Any]
        for attempt_index in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if not _is_retryable_http_status(int(exc.code)) or attempt_index + 1 >= attempts:
                    raise
                time.sleep(_retry_delay(self.retry_backoff_seconds, attempt_index))
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt_index + 1 >= attempts:
                    raise
                time.sleep(_retry_delay(self.retry_backoff_seconds, attempt_index))
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {payload}")
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return {"result": []}
        result = data.get("result", [])
        return {"result": result if isinstance(result, list) else []}


@dataclass(frozen=True)
class PrometheusTarget:
    cluster: str
    prometheus_url: str
    namespace_regex: str
    bearer_token: str
    basic_auth: str
    history_days: int
    step_seconds: int
    request_timeout_seconds: int
    rate_window: str = "15m"


ContainerKey = Tuple[str, str, str]
WorkloadKey = Tuple[str, str, str]
BYTES_PER_GIB = 1024 ** 3


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cluster_fetch_result(
    cluster: str,
    status: str,
    *,
    resources_fetched: int = 0,
    elapsed_seconds: Optional[float] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "cluster": str(cluster),
        "status": "success" if status == "success" else "failed",
        "resources_fetched": max(0, int(resources_fetched)),
        "elapsed_seconds": round(float(elapsed_seconds), 2) if elapsed_seconds is not None else None,
        "error": str(error) if error else None,
    }


def _missing_cluster_result(cluster: str) -> Dict[str, Any]:
    return _cluster_fetch_result(
        cluster,
        "failed",
        error=f"未找到 K8S Prometheus 集群配置: {cluster}",
    )


def _fetch_error_text(exc: Exception) -> str:
    """把重试次数并入错误消息，便于在更新历史里区分偶发故障和持续故障。"""
    attempts = getattr(exc, "attempts", 1)
    if isinstance(exc, K8SWorkloadAggregationError) and attempts > 1:
        return f"{exc}（已连续尝试 {attempts} 次）"
    return str(exc)


def _fetch_target_with_retry(
    target: PrometheusTarget,
    limit: int,
    *,
    history_hours: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """聚合不出 Workload 时整轮重试该集群，最多 AGGREGATION_MAX_ATTEMPTS 次。

    只重试 K8SWorkloadAggregationError：HTTP 层已有 request_max_attempts 重试，
    认证或表达式错误重试也不会变好。
    """
    max_attempts = max(1, int(AGGREGATION_MAX_ATTEMPTS))
    attempt = 0
    while True:
        attempt += 1
        try:
            return _fetch_target(target, limit, history_hours=history_hours)
        except K8SWorkloadAggregationError as exc:
            exc.attempts = attempt
            if attempt >= max_attempts:
                logger.error(
                    "[k8s_prometheus] fetch target gave up after %d attempts: cluster=%s reason=%s",
                    max_attempts,
                    target.cluster,
                    exc,
                )
                raise
            delay = AGGREGATION_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "[k8s_prometheus] fetch target attempt %d/%d aggregated no workload, "
                "retrying in %.1fs: cluster=%s reason=%s",
                attempt,
                max_attempts,
                delay,
                target.cluster,
                exc,
            )
            time.sleep(delay)


def fetch_k8s_workload_prometheus_result(
    *,
    resources: int,
    n: int,
    freq: str,
    clusters: Optional[Iterable[str]] = None,
    history_hours: Optional[float] = None,
    history_hours_by_cluster: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Fetch K8S Workloads and report the result of every target cluster.

    ``resources <= 0`` means unlimited, which is useful for production upsert.
    The ``n`` and ``freq`` arguments are accepted for compatibility with the
    generic data provider interface.
    提供 history_hours_by_cluster 时，未包含的集群使用完整历史窗口。
    """
    cfg = settings.k8s_prometheus
    targets = _resolve_targets()
    wanted = {str(x).strip() for x in clusters or [] if str(x).strip()}
    if wanted:
        targets = [target for target in targets if target.cluster in wanted]
    missing = sorted(wanted - {target.cluster for target in targets})
    if not targets and not missing:
        raise ValueError(
            "请配置 settings.k8s_prometheus.clusters，"
            "或环境变量 K8S_PROMETHEUS_CLUSTERS"
        )

    limit = int(resources or 0)
    out: List[Dict[str, Any]] = []
    cluster_results: List[Dict[str, Any]] = []
    started_at = _utc_timestamp()
    started_perf = time.perf_counter()
    logger.info(
        "[k8s_prometheus] fetch started: clusters=%s targets=%d resources_limit=%s history_hours=%s started_at=%s",
        ",".join(target.cluster for target in targets),
        len(targets),
        "unlimited" if limit <= 0 else str(limit),
        history_hours if history_hours is not None else "default",
        started_at,
    )
    for target in targets:
        target_history_hours = (history_hours_by_cluster.get(target.cluster)
                                if history_hours_by_cluster is not None else history_hours)
        if bool(cfg.fail_fast) and any(item["status"] == "failed" for item in cluster_results):
            cluster_results.append(
                _cluster_fetch_result(
                    target.cluster,
                    "failed",
                    error="因 fail_fast 在前序集群失败后未执行",
                )
            )
            continue
        target_started_at = _utc_timestamp()
        target_started_perf = time.perf_counter()
        fetched_count = 0
        try:
            remaining = 0 if limit <= 0 else max(0, limit - len(out))
            if limit > 0 and remaining <= 0:
                break
            logger.info(
                "[k8s_prometheus] fetch target started: cluster=%s url=%s resources_limit=%s "
                "history_hours=%s started_at=%s",
                target.cluster,
                target.prometheus_url,
                "unlimited" if remaining <= 0 else str(remaining),
                target_history_hours if target_history_hours is not None else "default",
                target_started_at,
            )
            items = _fetch_target_with_retry(target, remaining, history_hours=target_history_hours)
            fetched_count = len(items)
            if not items:
                # _fetch_target 已按断点抛出带根因的错误，这里只兜底意外情况。
                raise K8SWorkloadAggregationError(
                    f"集群 {target.cluster} 未返回可聚合的 K8S Workload（未取得细分原因）"
                )
            out.extend(items)
            target_finished_at = _utc_timestamp()
            cluster_results.append(
                _cluster_fetch_result(
                    target.cluster,
                    "success",
                    resources_fetched=fetched_count,
                    elapsed_seconds=time.perf_counter() - target_started_perf,
                )
            )
            logger.info(
                "[k8s_prometheus] fetch target finished: cluster=%s resources=%d elapsed=%.2fs "
                "started_at=%s finished_at=%s",
                target.cluster,
                fetched_count,
                time.perf_counter() - target_started_perf,
                target_started_at,
                target_finished_at,
            )
        except Exception as exc:
            target_finished_at = _utc_timestamp()
            error_text = _fetch_error_text(exc)
            cluster_results.append(
                _cluster_fetch_result(
                    target.cluster,
                    "failed",
                    resources_fetched=fetched_count,
                    elapsed_seconds=time.perf_counter() - target_started_perf,
                    error=error_text,
                )
            )
            logger.error(
                "[k8s_prometheus] fetch target failed: cluster=%s resources=%d elapsed=%.2fs "
                "started_at=%s finished_at=%s error=%s",
                target.cluster,
                fetched_count,
                time.perf_counter() - target_started_perf,
                target_started_at,
                target_finished_at,
                error_text,
            )

    cluster_results.extend(_missing_cluster_result(cluster) for cluster in missing)

    finished_at = _utc_timestamp()
    logger.info(
        "[k8s_prometheus] fetch finished: resources=%d targets=%d errors=%d elapsed=%.2fs "
        "started_at=%s finished_at=%s",
        len(out),
        len(targets),
        sum(1 for item in cluster_results if item["status"] == "failed"),
        time.perf_counter() - started_perf,
        started_at,
        finished_at,
    )
    return {"items": out, "cluster_results": cluster_results}


def k8s_workload_prometheus_provider(
    *,
    resources: int,
    n: int,
    freq: str,
    clusters: Optional[Iterable[str]] = None,
    history_hours: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Compatibility wrapper returning only fetched K8S Workload items."""
    result = fetch_k8s_workload_prometheus_result(
        resources=resources,
        n=n,
        freq=freq,
        clusters=clusters,
        history_hours=history_hours,
    )
    return list(result["items"])


def diagnose_k8s_prometheus(
    *,
    clusters: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Check whether configured Prometheus targets can produce K8S workloads."""
    targets = _resolve_targets()
    wanted = {str(x).strip() for x in clusters or [] if str(x).strip()}
    if wanted:
        targets = [target for target in targets if target.cluster in wanted]
        missing = wanted - {target.cluster for target in targets}
        if missing:
            raise ValueError(f"未找到 K8S Prometheus 集群配置: {', '.join(sorted(missing))}")
    reports = [_diagnose_target(target) for target in targets]
    ok = bool(reports) and all(bool(report.get("ok")) for report in reports)
    return {
        "ok": ok,
        "clusters_checked": len(reports),
        "clusters": reports,
    }


def _diagnose_target(target: PrometheusTarget) -> Dict[str, Any]:
    client = PrometheusClient(
        base_url=target.prometheus_url,
        bearer_token=target.bearer_token,
        basic_auth=target.basic_auth,
        timeout_seconds=int(target.request_timeout_seconds),
        max_attempts=int(settings.k8s_prometheus.request_max_attempts),
        retry_backoff_seconds=float(settings.k8s_prometheus.retry_backoff_seconds),
        range_query_chunk_hours=int(settings.k8s_prometheus.range_query_chunk_hours),
    )
    step = int(target.step_seconds)
    end = time.time()
    start = end - max(step * 2, 600)
    selector = 'container!="",container!="POD",pod!=""'
    if target.namespace_regex:
        selector += f',namespace=~"{target.namespace_regex}"'
    owner_selector = 'pod!=""'
    replicaset_owner_selector = ""
    if target.namespace_regex:
        owner_selector += f',namespace=~"{target.namespace_regex}"'
        replicaset_owner_selector = f'namespace=~"{target.namespace_regex}"'

    warnings: List[str] = []
    errors: List[str] = []
    query_counts: Dict[str, int] = {}
    try:
        cpu_usage_rows = client.query_range(
            f"rate(container_cpu_usage_seconds_total{{{selector}}}[{target.rate_window}])",
            start=start,
            end=end,
            step=step,
        )
        mem_usage_rows = client.query_range(
            f"container_memory_working_set_bytes{{{selector}}}",
            start=start,
            end=end,
            step=step,
        )
        cpu_usage = _range_by_key(cpu_usage_rows)
        mem_usage = _range_by_key(mem_usage_rows)
        query_counts["cpu_usage_series"] = len(cpu_usage)
        query_counts["memory_usage_series"] = len(mem_usage)

        owner_diagnostics: Dict[str, Any] = {}
        pod_owners_raw = _pod_owner_values(client, owner_selector, owner_diagnostics)
        replicaset_owners = _replicaset_owner_values(client, replicaset_owner_selector)
        pod_owners = _resolve_controller_owners(pod_owners_raw, replicaset_owners)
        replica_values = _replica_values_by_workload(client, replicaset_owner_selector)
        query_counts["pod_owner_rows"] = len(pod_owners_raw)
        query_counts["replicaset_owner_rows"] = len(replicaset_owners)
        query_counts["workload_replica_rows"] = len(replica_values)

        all_container_keys = set(cpu_usage) | set(mem_usage)
        workload_keys = {_workload_key(key, pod_owners) for key in all_container_keys}
        workload_keys.discard(None)
        orphan_keys = [key for key in all_container_keys if _workload_key(key, pod_owners) is None]
        query_counts["container_series"] = len(all_container_keys)
        query_counts["workloads_resolved"] = len(workload_keys)
        query_counts["orphan_container_series"] = len(orphan_keys)

        pod_owner_kinds = {kind for kind, _name in pod_owners_raw.values()}
        if "ReplicaSet" in pod_owner_kinds and not replicaset_owners:
            warnings.append("kube_pod_owner 返回 ReplicaSet，但 kube_replicaset_owner 无结果，Deployment 会退化为 ReplicaSet 粒度")

        cpu_request = _instant_values(client, [
            f"kube_pod_container_resource_requests_cpu_cores{{{selector}}}",
            f'kube_pod_container_resource_requests{{{selector},resource="cpu",unit="core"}}',
        ])
        cpu_limit = _instant_values(client, [
            f"kube_pod_container_resource_limits_cpu_cores{{{selector}}}",
            f'kube_pod_container_resource_limits{{{selector},resource="cpu",unit="core"}}',
        ])
        mem_request = _instant_values(client, [
            f"kube_pod_container_resource_requests_memory_bytes{{{selector}}}",
            f'kube_pod_container_resource_requests{{{selector},resource="memory",unit="byte"}}',
        ])
        mem_limit = _instant_values(client, [
            f"kube_pod_container_resource_limits_memory_bytes{{{selector}}}",
            f'kube_pod_container_resource_limits{{{selector},resource="memory",unit="byte"}}',
        ])
        query_counts["cpu_request_series"] = len(cpu_request)
        query_counts["cpu_limit_series"] = len(cpu_limit)
        query_counts["memory_request_series"] = len(mem_request)
        query_counts["memory_limit_series"] = len(mem_limit)

        if not cpu_usage:
            errors.append("CPU 使用率查询无结果")
        if not mem_usage:
            errors.append("内存使用率查询无结果")
        if not pod_owners_raw:
            errors.append("kube_pod_owner 查询无结果，无法聚合到控制器粒度")
            owner_query_errors = [
                str(item) for item in owner_diagnostics.get("owner_query_errors") or []
            ]
            if owner_query_errors:
                errors.append(f"kube_pod_owner 查询异常: {' | '.join(owner_query_errors)}")
        if all_container_keys and not workload_keys:
            errors.append("未解析出任何 Workload；请检查 kube_pod_owner/kube_replicaset_owner 标签")
        if not cpu_request and not cpu_limit:
            warnings.append("CPU request/limit 均无结果，CPU 只能按原始 cores 趋势分析")
        if not mem_request and not mem_limit:
            warnings.append("内存 request/limit 均无结果，内存只能按原始 GB 趋势分析")
        if orphan_keys:
            warnings.append(f"{len(orphan_keys)} 条容器序列缺少 owner，已从 Workload 聚合中排除")

        sample_workloads = [
            {
                "namespace": namespace,
                "workload_kind": owner_kind,
                "workload_name": owner_name,
            }
            for namespace, owner_kind, owner_name in sorted(workload_keys)[:5]
        ]
        ok = not errors
        return {
            "cluster": target.cluster,
            "prometheus_url": target.prometheus_url,
            "namespace_regex": target.namespace_regex,
            "ok": ok,
            "warnings": warnings,
            "errors": errors,
            "counts": query_counts,
            "sample_workloads": sample_workloads,
        }
    except Exception as exc:
        return {
            "cluster": target.cluster,
            "prometheus_url": target.prometheus_url,
            "namespace_regex": target.namespace_regex,
            "ok": False,
            "warnings": warnings,
            "errors": [str(exc)],
            "counts": query_counts,
            "sample_workloads": [],
        }


def _ensure_aggregatable(
    target: PrometheusTarget,
    *,
    cpu_series: int,
    memory_series: int,
    container_series: int,
    pod_owner_rows: int,
    workloads_resolved: int,
    orphan_series: int,
    owner_query_errors: List[str],
) -> None:
    """按聚合链路的断点抛出带根因的错误，不再统一报“未返回可聚合的 Workload”。"""
    if container_series <= 0:
        raise K8SWorkloadAggregationError(
            f"集群 {target.cluster} 的 Prometheus 未返回容器使用率序列"
            f"（cpu_series={cpu_series}, memory_series={memory_series}）："
            f"请检查 cAdvisor 抓取、namespace_regex='{target.namespace_regex}' "
            f"与 rate_window='{target.rate_window}'"
        )
    if pod_owner_rows <= 0:
        detail = f"；owner 查询异常: {' | '.join(owner_query_errors)}" if owner_query_errors else ""
        raise K8SWorkloadAggregationError(
            f"集群 {target.cluster} 的 kube_pod_owner 查询无结果，"
            f"无法把 {container_series} 条容器序列聚合到控制器粒度"
            "（瞬时查询只回看约 5 分钟，kube-state-metrics 未上报或刚重启时即为空）"
            f"{detail}"
        )
    if workloads_resolved <= 0:
        raise K8SWorkloadAggregationError(
            f"集群 {target.cluster} 未解析出任何 Workload："
            f"{orphan_series}/{container_series} 条容器序列的 (namespace, pod) "
            "在 kube_pod_owner 中没有匹配项，请检查两侧标签是否一致"
        )


def fetch_single_k8s_workload(resource: Dict[str, Any]) -> Dict[str, Any]:
    spec = resource.get("spec", {})
    cluster = str(spec.get("cluster") or "")
    if not all(spec.get(key) for key in ("namespace", "workload_kind", "workload_name")):
        raise ValueError("Workload 缺少命名空间、类型或名称，无法定向拉取")
    targets = [target for target in _resolve_targets() if target.cluster == cluster]
    if len(targets) != 1:
        raise ValueError(f"未找到唯一的集群采集配置：{cluster}")
    namespace = str(spec["namespace"])
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace):
        raise ValueError("Workload 命名空间格式无效")
    target = replace(targets[0], namespace_regex=namespace)
    items = _fetch_target(target, 0, workload=spec)
    selected = [item for item in items if item["resource_id"] == resource["resource_id"]]
    if len(selected) != 1:
        raise ValueError("未拉取到当前 Workload 的有效指标，请检查历史归属和采集数据")
    return selected[0]


def _fetch_target(
    target: PrometheusTarget,
    limit: int,
    *,
    history_hours: Optional[float] = None,
    workload: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    client = PrometheusClient(
        base_url=target.prometheus_url,
        bearer_token=target.bearer_token,
        basic_auth=target.basic_auth,
        timeout_seconds=int(target.request_timeout_seconds),
        max_attempts=int(settings.k8s_prometheus.request_max_attempts),
        retry_backoff_seconds=float(settings.k8s_prometheus.retry_backoff_seconds),
        range_query_chunk_hours=int(settings.k8s_prometheus.range_query_chunk_hours),
    )
    end = time.time()
    if history_hours is not None and float(history_hours) > 0:
        start = end - float(history_hours) * 3600.0
    else:
        start = end - int(target.history_days) * 86400
    step = int(target.step_seconds)
    max_interpolation_gap_steps = int(
        settings.k8s_prometheus.max_interpolation_gap_steps
    )
    selector = 'container!="",container!="POD",pod!=""'
    if target.namespace_regex:
        selector += f',namespace=~"{target.namespace_regex}"'
    owner_selector = 'pod!=""'
    replicaset_owner_selector = ""
    if target.namespace_regex:
        owner_selector += f',namespace=~"{target.namespace_regex}"'
        replicaset_owner_selector = f'namespace=~"{target.namespace_regex}"'

    if workload is not None:
        current_pods = _pod_owner_values(client, owner_selector)
        current_rs = _replicaset_owner_values(client, replicaset_owner_selector)
        historical = _historical_workload_owners(client, owner_selector, replicaset_owner_selector,
                                                start, end, step, current_pods, current_rs)
        pods = sorted(pod for (namespace, pod), (kind, name) in historical.items()
                      if namespace == workload["namespace"] and kind.lower() == str(workload["workload_kind"]).lower()
                      and name == workload["workload_name"])
        if not pods:
            raise ValueError("历史与当前归属中均未找到该 Workload 的 Pod")
        selector += ",pod=~" + json.dumps("|".join(re.escape(pod) for pod in pods))

    cpu_usage = _range_by_key(
        client.query_range(
            f"rate(container_cpu_usage_seconds_total{{{selector}}}[{target.rate_window}])",
            start=start,
            end=end,
            step=step,
        )
    )
    mem_usage = _range_by_key(
        client.query_range(
            f"container_memory_working_set_bytes{{{selector}}}",
            start=start,
            end=end,
            step=step,
        )
    )
    cpu_request = _instant_values(client, [
        f"kube_pod_container_resource_requests_cpu_cores{{{selector}}}",
        f'kube_pod_container_resource_requests{{{selector},resource="cpu",unit="core"}}',
    ])
    cpu_limit = _instant_values(client, [
        f"kube_pod_container_resource_limits_cpu_cores{{{selector}}}",
        f'kube_pod_container_resource_limits{{{selector},resource="cpu",unit="core"}}',
    ])
    mem_request = _instant_values(client, [
        f"kube_pod_container_resource_requests_memory_bytes{{{selector}}}",
        f'kube_pod_container_resource_requests{{{selector},resource="memory",unit="byte"}}',
    ])
    mem_limit = _instant_values(client, [
        f"kube_pod_container_resource_limits_memory_bytes{{{selector}}}",
        f'kube_pod_container_resource_limits{{{selector},resource="memory",unit="byte"}}',
    ])
    owner_diagnostics: Dict[str, Any] = {}
    pod_owners_raw = _pod_owner_values(client, owner_selector, owner_diagnostics)
    replicaset_owners = _replicaset_owner_values(client, replicaset_owner_selector)
    pod_owners = (
        _resolve_controller_owners(pod_owners_raw, replicaset_owners)
        if replicaset_owners
        else pod_owners_raw
    )
    replica_values = _replica_values_by_workload(client, replicaset_owner_selector)

    container_keys = set(cpu_usage) | set(mem_usage)
    current_pod_owners = dict(pod_owners)
    pod_owners = _historical_workload_owners(
        client, owner_selector, replicaset_owner_selector, start, end, step,
        pod_owners_raw, replicaset_owners,
    )
    logger.info("[k8s_prometheus] 历史归属恢复: cluster=%s current_pods=%d historical_pods=%d recovered_pods=%d",
                target.cluster, len(current_pod_owners), len(pod_owners), len(set(pod_owners) - set(current_pod_owners)))
    owner_by_container = {key: _workload_key(key, pod_owners) for key in container_keys}
    orphan_keys = [key for key, workload_key in owner_by_container.items() if workload_key is None]
    workload_keys = sorted(
        workload_key for workload_key in set(owner_by_container.values()) if workload_key is not None
    )
    owner_query_errors = [str(item) for item in owner_diagnostics.get("owner_query_errors") or []]
    logger.info(
        "[k8s_prometheus] fetch target aggregation: cluster=%s cpu_series=%d memory_series=%d "
        "container_series=%d pod_owner_rows=%d replicaset_owner_rows=%d workloads_resolved=%d "
        "orphan_container_series=%d owner_query_errors=%s",
        target.cluster,
        len(cpu_usage),
        len(mem_usage),
        len(container_keys),
        len(pod_owners_raw),
        len(replicaset_owners),
        len(workload_keys),
        len(orphan_keys),
        owner_query_errors or "none",
    )
    _ensure_aggregatable(
        target,
        cpu_series=len(cpu_usage),
        memory_series=len(mem_usage),
        container_series=len(container_keys),
        pod_owner_rows=len(pod_owners_raw),
        workloads_resolved=len(workload_keys),
        orphan_series=len(orphan_keys),
        owner_query_errors=owner_query_errors,
    )
    cpu_usage_by_workload = _sum_series_by_workload(cpu_usage, target.cluster, pod_owners)
    mem_usage_by_workload = _sum_series_by_workload(mem_usage, target.cluster, pod_owners)
    cpu_usage_by_container = _sum_series_by_workload_container(cpu_usage, pod_owners)
    mem_usage_by_container = _sum_series_by_workload_container(mem_usage, pod_owners)
    cpu_request_by_container = _values_by_workload_container(cpu_request, pod_owners)
    cpu_limit_by_container = _values_by_workload_container(cpu_limit, pod_owners)
    mem_request_by_container = _values_by_workload_container(mem_request, pod_owners)
    mem_limit_by_container = _values_by_workload_container(mem_limit, pod_owners)
    current_metadata = _workload_metadata(target.cluster, container_keys, current_pod_owners, cpu_usage, mem_usage)
    metadata_by_workload = _workload_metadata(
        target.cluster,
        container_keys,
        pod_owners,
        cpu_usage,
        mem_usage,
    )
    capacity_history, evidence_errors = _scaling_capacity_history(client, selector, start, end, step)
    normalized = {}
    current_capacities = {("cpu", "request"): cpu_request, ("cpu", "limit"): cpu_limit,
                          ("memory", "request"): mem_request, ("memory", "limit"): mem_limit}
    for metric, usage in (("cpu", cpu_usage), ("memory", mem_usage)):
        for basis in ("request", "limit"):
            if current_capacities[(metric, basis)] and not capacity_history.get((metric, basis)):
                raise RuntimeError(f"{metric}/{basis} 当前存在配置，但历史容量查询无结果；停止本轮写入，避免使用新规格重算旧利用率")
            normalized[(metric, basis)] = _historical_normalized_series(
                usage, capacity_history.get((metric, basis), {}), pod_owners, metric, basis,
            )
    scaling_series = _scaling_series_by_workload(cpu_usage, mem_usage, capacity_history, pod_owners)
    evidence_collected_at_ms = int(time.time() * 1000)

    out: List[Dict[str, Any]] = []
    for key in workload_keys:
        cpu_s = cpu_usage_by_workload.get(key)
        mem_s = mem_usage_by_workload.get(key)
        if cpu_s is None or mem_s is None:
            continue
        cpu_limit_metric, cpu_limit_norm = normalized[('cpu', 'limit')][(key, None)]
        cpu_request_metric, cpu_request_norm = normalized[('cpu', 'request')][(key, None)]
        mem_limit_metric, mem_limit_norm = normalized[('memory', 'limit')][(key, None)]
        mem_request_metric, mem_request_norm = normalized[('memory', 'request')][(key, None)]
        cpu_limit_quality = _data_quality(cpu_limit_norm, step)
        cpu_request_quality = _data_quality(cpu_request_norm, step)
        mem_limit_quality = _data_quality(mem_limit_norm, step)
        mem_request_quality = _data_quality(mem_request_norm, step)
        meta = metadata_by_workload.get(key, {})
        container_metrics: Dict[str, Dict[str, Any]] = {}
        observed_containers: Dict[str, Dict[str, Any]] = {}
        container_quality: Dict[str, Dict[str, Any]] = {}
        container_modes: Dict[str, Dict[str, str]] = {}
        container_names = sorted(meta.get("containers", []))
        for container in container_names:
            cpu_container_s = cpu_usage_by_container.get(key, {}).get(container)
            mem_container_s = mem_usage_by_container.get(key, {}).get(container)
            if cpu_container_s is None or mem_container_s is None:
                continue
            cpu_limit_container_metric, cpu_limit_container_norm = normalized[('cpu', 'limit')][(key, container)]
            cpu_request_container_metric, cpu_request_container_norm = normalized[('cpu', 'request')][(key, container)]
            mem_limit_container_metric, mem_limit_container_norm = normalized[('memory', 'limit')][(key, container)]
            mem_request_container_metric, mem_request_container_norm = normalized[('memory', 'request')][(key, container)]
            container_quality[container] = {
                "cpu_limit": _data_quality(cpu_limit_container_norm, step),
                "cpu_request": _data_quality(cpu_request_container_norm, step),
                "memory_limit": _data_quality(mem_limit_container_norm, step),
                "memory_request": _data_quality(mem_request_container_norm, step),
            }
            observed_containers[container] = {
                name: _series_payload(_regularize_series(series, step, 0))
                for name, series in (
                    ("cpu_limit", cpu_limit_container_norm),
                    ("cpu_request", cpu_request_container_norm),
                    ("memory_limit", mem_limit_container_norm),
                    ("memory_request", mem_request_container_norm),
                )
            }
            container_metrics[container] = {
                "cpu_limit": _series_payload(_regularize_series(
                    cpu_limit_container_norm, step, max_interpolation_gap_steps
                )),
                "cpu_request": _series_payload(_regularize_series(
                    cpu_request_container_norm, step, max_interpolation_gap_steps
                )),
                "memory_limit": _series_payload(_regularize_series(
                    mem_limit_container_norm, step, max_interpolation_gap_steps
                )),
                "memory_request": _series_payload(_regularize_series(
                    mem_request_container_norm, step, max_interpolation_gap_steps
                )),
            }
            container_modes[container] = {
                "cpu_limit": cpu_limit_container_metric,
                "cpu_request": cpu_request_container_metric,
                "memory_limit": mem_limit_container_metric,
                "memory_request": mem_request_container_metric,
            }
        observed_metrics = {
            name: _series_payload(_regularize_series(series, step, 0))
            for name, series in (
                ("cpu_limit", cpu_limit_norm), ("cpu_request", cpu_request_norm),
                ("memory_limit", mem_limit_norm), ("memory_request", mem_request_norm),
            )
        }
        cpu_limit_norm = _regularize_series(
            cpu_limit_norm, step, max_interpolation_gap_steps
        )
        cpu_request_norm = _regularize_series(
            cpu_request_norm, step, max_interpolation_gap_steps
        )
        mem_limit_norm = _regularize_series(
            mem_limit_norm, step, max_interpolation_gap_steps
        )
        mem_request_norm = _regularize_series(
            mem_request_norm, step, max_interpolation_gap_steps
        )
        namespace, owner_kind, owner_name = key
        meta = current_metadata.get(key, {})
        # 优先使用 kube-state-metrics 上报的控制器副本数（spec/status replicas），
        # 它来自 K8s API 是权威值；仅当 kube-state-metrics 无数据时才回退到
        # Prometheus 中有容器指标的 pod 数，避免某个 pod 未上报指标时低估副本数。
        kube_replicas = replica_values.get(key)
        if kube_replicas is not None and kube_replicas > 0:
            replicas_observed = kube_replicas
        else:
            replicas_observed = len(meta.get("pods", []))
        spec = {
            "cluster": target.cluster,
            "namespace": namespace,
            "owner_kind": owner_kind,
            "owner_name": owner_name,
            "workload_kind": owner_kind,
            "workload_name": owner_name,
            "pods_observed": sorted(meta.get("pods", [])),
            "containers_observed": sorted(meta.get("containers", [])),
            "replicas": replica_values.get(key),
            "replicas_observed": replicas_observed,
            "containers": _container_specs(
                sorted(meta.get("containers", [])),
                cpu_request_by_container.get(key, {}),
                cpu_limit_by_container.get(key, {}),
                mem_request_by_container.get(key, {}),
                mem_limit_by_container.get(key, {}),
            ),
            "cpu_limit_metric_mode": cpu_limit_metric,
            "cpu_request_metric_mode": cpu_request_metric,
            "memory_limit_metric_mode": mem_limit_metric,
            "memory_request_metric_mode": mem_request_metric,
        }
        nodes = sorted(meta.get("nodes", []))
        if nodes:
            spec["nodes"] = nodes
        item = {
            "resource_id": f"k8s:{target.cluster}:{namespace}:{owner_kind.lower()}:{owner_name}",
            "resource_type": "k8s_workload",
            "spec": spec,
            "metrics": {
                "cpu_limit": _series_payload(cpu_limit_norm),
                "cpu_request": _series_payload(cpu_request_norm),
                "memory_limit": _series_payload(mem_limit_norm),
                "memory_request": _series_payload(mem_request_norm),
            },
            "data_quality": {
                "cpu_limit": cpu_limit_quality,
                "cpu_request": cpu_request_quality,
                "memory_limit": mem_limit_quality,
                "memory_request": mem_request_quality,
            },
        }
        if container_metrics:
            item["container_metrics"] = container_metrics
            item["container_data_quality"] = container_quality
            item["container_metric_modes"] = container_modes
        item["observation_evidence"] = {
            "schema_version": 1,
            "source": "k8s_prometheus_unfilled",
            "resource_type": "k8s_workload",
            "spec": spec,
            "container_metric_modes": container_modes,
            "metrics": observed_metrics,
            "container_metrics": observed_containers,
        }
        item["scaling_evidence"] = {
            "schema_version": 1,
            "source": "k8s_prometheus_scaling_unfilled",
            "collected_at_ms": evidence_collected_at_ms,
            "spec": spec,
            "sample_interval_ms": step * 1000,
            "series": scaling_series.get(key, []),
            "query_errors": evidence_errors,
            "provenance": {
                "owner_mapping": "historical_range_unique_owner_with_current_fallback",
                "aggregation": "same_observed_members_at_exact_timestamp",
                "cpu_rate_window": target.rate_window,
                "orphan_container_series": len(orphan_keys),
            },
            "limitations": [
                "Historical ownership requires retained owner metrics; ambiguous ownership is excluded.",
                "Containers absent from all queried metrics at a timestamp cannot be detected; this is observed mapped capacity, not physical cluster utilization.",
            ],
        }
        out.append(item)
        if limit > 0 and len(out) >= limit:
            break
    if not out:
        raise K8SWorkloadAggregationError(
            f"集群 {target.cluster} 解析出 {len(workload_keys)} 个 Workload，"
            "但没有一个同时具备 CPU 与内存使用率序列，无法聚合"
        )
    return out


def _historical_workload_owners(client, pod_selector, rs_selector, start, end, step, current_pods, current_rs):
    """恢复已退出 Pod 的历史归属；同名对象归属有冲突时不猜测。"""
    def collect(metric_name, selector, name_label, current):
        candidates = {key: {value} for key, value in current.items()}
        prefix = selector + "," if selector else ""
        history_found = False
        for query in (f'{metric_name}{{{prefix}owner_is_controller="true"}}', f"{metric_name}{{{selector}}}"):
            try:
                rows = client.query_range(query, start=start, end=end, step=step)
            except Exception as exc:
                logger.warning("[k8s_prometheus] 历史归属查询失败: metric=%s error=%s", metric_name, exc)
                continue
            if not rows:
                continue
            history_found = True
            for row in rows:
                labels = row.get("metric", {})
                key = (labels.get("namespace"), labels.get(name_label))
                owner = (labels.get("owner_kind"), labels.get("owner_name"))
                if all(key) and all(owner) and any(float(value) > 0 for _, value in row.get("values", [])):
                    candidates.setdefault(key, set()).add(owner)
            break
        if not history_found:
            logger.warning("[k8s_prometheus] 无历史归属数据，仅可使用当前归属: metric=%s", metric_name)
        conflicts = {key for key, owners in candidates.items() if len(owners) != 1}
        if conflicts:
            logger.warning("[k8s_prometheus] 排除历史归属冲突: metric=%s objects=%d", metric_name, len(conflicts))
        return {key: next(iter(owners)) for key, owners in candidates.items() if key not in conflicts}, conflicts

    pods, _ = collect("kube_pod_owner", pod_selector, "pod", current_pods)
    replica_sets, conflicts = collect("kube_replicaset_owner", rs_selector, "replicaset", current_rs)
    result = {}
    for key, owner in pods.items():
        if owner[0].lower() == "replicaset":
            rs_key = (key[0], owner[1])
            if rs_key in conflicts:
                continue
            owner = replica_sets.get(rs_key, owner)
        result[key] = owner
    return result


def _historical_normalized_series(usage, capacity, owners, metric, basis):
    """用同一时间、同一组容器的使用量和历史容量计算比例，不套用当前规格。"""
    groups = {}
    for member in usage.keys() | capacity.keys():
        workload = _workload_key(member, owners)
        if workload is not None:
            for container in (None, member[2]):
                groups.setdefault((workload, container), set()).add(member)
    out = {}
    ratio_name = f"cpu_usage/cpu_{basis}" if metric == "cpu" else f"memory_working_set/memory_{basis}"
    raw_name = "cpu_usage_cores" if metric == "cpu" else "memory_working_set_gb"
    for group, members in groups.items():
        used = pd.DataFrame({key: usage[key] for key in members if key in usage}).sort_index()
        caps = pd.DataFrame({key: capacity[key] for key in members if key in capacity}).sort_index()
        if used.empty:
            continue
        if caps.empty or not caps.gt(0).any().any():
            out[group] = (raw_name, used.sum(axis=1, min_count=1) / (1 if metric == "cpu" else BYTES_PER_GIB))
            continue
        columns = sorted(members)
        index = used.index.union(caps.index)
        used = used.reindex(index=index, columns=columns)
        caps = caps.reindex(index=index, columns=columns)
        positive = caps.gt(0) & np.isfinite(caps)
        valid_usage = used.ge(0) & np.isfinite(used)
        # 有基线的成员缺失历史容量或使用量时留缺口，不能变成单副本假低谷。
        unknown_capacity = valid_usage & caps.isna() & positive.any(axis=0)
        valid = positive.any(axis=1) & ~(positive & ~valid_usage).any(axis=1) & ~unknown_capacity.any(axis=1)
        ratio = used.where(positive).sum(axis=1, min_count=1) / caps.where(positive).sum(axis=1, min_count=1)
        out[group] = (ratio_name, ratio.where(valid).dropna())
    return out


def _scaling_capacity_history(
    client: PrometheusClient, selector: str, start: float, end: float, step: int,
) -> Tuple[Dict[Tuple[str, str], Dict[ContainerKey, pd.Series]], List[str]]:
    history: Dict[Tuple[str, str], Dict[ContainerKey, pd.Series]] = {}
    errors: List[str] = []
    for metric, resource, suffix, unit in (
        ("cpu", "cpu", "cpu_cores", "core"),
        ("memory", "memory", "memory_bytes", "byte"),
    ):
        for basis in ("request", "limit"):
            prefix = f"kube_pod_container_resource_{basis}s"
            # Prefer the legacy metric where present, and deduplicate exporter labels.
            query = (
                f"max by (namespace,pod,container) ({prefix}_{suffix}{{{selector}}})"
                " or on (namespace,pod,container) "
                f'max by (namespace,pod,container) ({prefix}{{{selector},resource="{resource}",unit="{unit}"}})'
            )
            try:
                history[(metric, basis)] = _range_by_key(
                    client.query_range(query, start=start, end=end, step=step)
                )
                if not history[(metric, basis)]:
                    errors.append(f"{metric}/{basis}: historical capacity unavailable")
            except Exception as exc:
                history[(metric, basis)] = {}
                detail = f"{metric}/{basis}: {type(exc).__name__}: {exc}"
                errors.append(detail)
                logger.warning("[k8s_prometheus] scaling evidence unavailable: %s", detail)
    return history, errors


def _scaling_series_by_workload(
    cpu_usage: Dict[ContainerKey, pd.Series],
    mem_usage: Dict[ContainerKey, pd.Series],
    capacity_history: Dict[Tuple[str, str], Dict[ContainerKey, pd.Series]],
    pod_owners: Dict[Tuple[str, str], Tuple[str, str]],
) -> Dict[WorkloadKey, List[Dict[str, Any]]]:
    sources = [cpu_usage, mem_usage, *capacity_history.values()]
    grouped: Dict[Tuple[WorkloadKey, str], Set[ContainerKey]] = {}
    for source in sources:
        for member in source:
            workload = _workload_key(member, pod_owners)
            if workload is not None:
                grouped.setdefault((workload, member[2]), set()).add(member)
    out: Dict[WorkloadKey, List[Dict[str, Any]]] = {}
    for (workload, container), members in sorted(grouped.items()):
        # A sample in any metric establishes membership, even when its value is NaN.
        presence = {}
        for member in sorted(members):
            indices = [source[member].index for source in sources if member in source]
            index = indices[0]
            for other in indices[1:]:
                index = index.union(other)
            presence[member] = pd.Series(True, index=index)
        active = pd.DataFrame(presence).notna().sort_index()
        expected = active.sum(axis=1)
        for metric, usage, divisor, unit in (
            ("cpu", cpu_usage, 1, "cores"),
            ("memory", mem_usage, BYTES_PER_GIB, "GiB"),
        ):
            for basis in ("request", "limit"):
                capacity = capacity_history.get((metric, basis), {})
                frames = [pd.DataFrame({member: source[member] for member in members if member in source})
                          .reindex(index=active.index, columns=active.columns)
                          for source in (usage, capacity)]
                valid = expected.gt(0)
                totals = []
                for frame in frames:
                    usable = np.isfinite(frame) & frame.ge(0)
                    valid &= (usable & active).sum(axis=1).eq(expected)
                    totals.append(frame.where(active & usable).sum(axis=1, min_count=1) / divisor)
                    valid &= np.isfinite(totals[-1])
                values = [[float(value) if ok else None for value, ok in zip(total, valid)] for total in totals]
                out.setdefault(workload, []).append({
                    "container": container, "metric": metric, "basis": basis, "unit": unit,
                    "timestamps": (active.index.as_unit("ns").view("int64") // 1_000_000).astype(int).tolist(),
                    "usage": values[0], "capacity": values[1],
                })
    return out


def _resolve_targets() -> List[PrometheusTarget]:
    cfg = settings.k8s_prometheus
    env_targets = _targets_from_env()
    file_targets = _targets_from_file()
    configured = file_targets or env_targets or list(cfg.clusters)

    targets: List[PrometheusTarget] = []
    invalid: List[str] = []
    for idx, item in enumerate(configured, start=1):
        data = _target_to_dict(item)
        cluster = str(data.get("cluster") or "").strip()
        url = str(data.get("prometheus_url") or "").strip()
        if not cluster or not url:
            invalid.append(f"第 {idx} 项缺少 cluster 或 prometheus_url")
            continue
        targets.append(
            PrometheusTarget(
                cluster=cluster,
                prometheus_url=url,
                namespace_regex=str(data.get("namespace_regex") or cfg.namespace_regex or ""),
                bearer_token=str(data.get("bearer_token") or ""),
                basic_auth=str(data.get("basic_auth") or ""),
                history_days=int(cfg.history_days),
                step_seconds=int(cfg.step_seconds),
                request_timeout_seconds=int(cfg.request_timeout_seconds),
                rate_window=str(data.get("rate_window") or cfg.rate_window or "15m").strip(),
            )
        )
    if invalid:
        raise ValueError("K8S Prometheus 集群配置无效: " + "；".join(invalid))
    return targets


def _targets_from_env() -> List[Dict[str, Any]]:
    raw = os.getenv("K8S_PROMETHEUS_CLUSTERS", "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("K8S_PROMETHEUS_CLUSTERS 必须是 JSON 数组或对象") from exc
    if isinstance(payload, list):
        if not all(isinstance(x, dict) for x in payload):
            raise ValueError("K8S_PROMETHEUS_CLUSTERS 数组元素必须都是对象")
        return payload
    if isinstance(payload, dict):
        out: List[Dict[str, Any]] = []
        for cluster, value in payload.items():
            if isinstance(value, str):
                out.append({"cluster": cluster, "prometheus_url": value})
            elif isinstance(value, dict):
                out.append({"cluster": cluster, **value})
        return out
    raise ValueError("K8S_PROMETHEUS_CLUSTERS 必须是 JSON 数组或对象")


def _targets_from_file() -> List[Dict[str, Any]]:
    if not K8S_PROMETHEUS_CONFIG_PATH.exists():
        return []
    try:
        return read_k8s_prometheus_clusters(K8S_PROMETHEUS_CONFIG_PATH)
    except ClusterConfigValidationError as exc:
        raise ValueError(str(exc)) from exc


def _target_to_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    return {
        "cluster": getattr(item, "cluster", ""),
        "prometheus_url": getattr(item, "prometheus_url", ""),
        "namespace_regex": getattr(item, "namespace_regex", ""),
        "bearer_token": getattr(item, "bearer_token", ""),
        "basic_auth": getattr(item, "basic_auth", ""),
        "rate_window": getattr(item, "rate_window", ""),
    }


def _key(metric: Dict[str, Any]) -> ContainerKey:
    return (
        str(metric.get("namespace") or ""),
        str(metric.get("pod") or ""),
        str(metric.get("container") or ""),
    )


def _range_by_key(result: Iterable[Dict[str, Any]]) -> Dict[ContainerKey, pd.Series]:
    out: Dict[ContainerKey, pd.Series] = {}
    for row in result:
        metric = row.get("metric", {})
        values = row.get("values", [])
        key = _key(metric if isinstance(metric, dict) else {})
        if not all(key) or not isinstance(values, list):
            continue
        idx = [float(x[0]) for x in values if isinstance(x, list) and len(x) >= 2]
        vals = [float(x[1]) for x in values if isinstance(x, list) and len(x) >= 2]
        if not idx or len(idx) != len(vals):
            continue
        s = pd.Series(vals, index=pd.to_datetime(idx, unit="s", utc=True)).sort_index()
        s.attrs["labels"] = metric if isinstance(metric, dict) else {}
        out[key] = s
    return out


def _instant_values(client: PrometheusClient, queries: List[str]) -> Dict[ContainerKey, float]:
    merged: Dict[ContainerKey, float] = {}
    for query in queries:
        try:
            rows = client.query(query)
        except Exception as exc:
            logger.warning(
                "[k8s_prometheus] request/limit 瞬时查询失败，改用下一个候选查询: query=%s error=%s",
                query,
                exc,
            )
            continue
        for row in rows:
            metric = row.get("metric", {})
            value = row.get("value", [])
            key = _key(metric if isinstance(metric, dict) else {})
            if not all(key) or not isinstance(value, list) or len(value) < 2:
                continue
            try:
                v = float(value[1])
            except Exception:
                continue
            if v > 0:
                merged[key] = v
        if merged:
            return merged
    return merged


def _pod_owner_values(
    client: PrometheusClient,
    selector: str,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[Tuple[str, str], Tuple[str, str]]:
    queries = [
        f'kube_pod_owner{{{selector},owner_is_controller="true"}}',
        f"kube_pod_owner{{{selector}}}",
    ]
    owners: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for query in queries:
        try:
            rows = client.query(query)
        except Exception as exc:
            # 这里的异常以前被静默吞掉，最终只会表现为“聚合不出 Workload”。
            # 记录并回传原因，才能在日志和错误消息里看到真实根因。
            logger.warning(
                "[k8s_prometheus] kube_pod_owner 查询失败，改用下一个候选查询: query=%s error=%s",
                query,
                exc,
            )
            if diagnostics is not None:
                # 两个候选查询通常是同一个根因，去重后消息更短。
                recorded_errors = diagnostics.setdefault("owner_query_errors", [])
                detail = f"{type(exc).__name__}: {exc}"
                if detail not in recorded_errors:
                    recorded_errors.append(detail)
            continue
        for row in rows:
            metric = row.get("metric", {})
            if not isinstance(metric, dict):
                continue
            namespace = str(metric.get("namespace") or "")
            pod = str(metric.get("pod") or "")
            owner_kind = str(metric.get("owner_kind") or "")
            owner_name = str(metric.get("owner_name") or "")
            if namespace and pod and owner_kind and owner_name:
                owners[(namespace, pod)] = (owner_kind, owner_name)
        if owners:
            return owners
    return owners


def _replicaset_owner_values(client: PrometheusClient, selector: str) -> Dict[Tuple[str, str], Tuple[str, str]]:
    prefix = f"{selector}," if selector else ""
    queries = [
        f'kube_replicaset_owner{{{prefix}owner_is_controller="true"}}',
        f"kube_replicaset_owner{{{selector}}}",
    ]
    owners: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for query in queries:
        try:
            rows = client.query(query)
        except Exception as exc:
            logger.warning(
                "[k8s_prometheus] kube_replicaset_owner 查询失败，改用下一个候选查询: query=%s error=%s",
                query,
                exc,
            )
            continue
        for row in rows:
            metric = row.get("metric", {})
            if not isinstance(metric, dict):
                continue
            namespace = str(metric.get("namespace") or "")
            replicaset = str(metric.get("replicaset") or metric.get("replica_set") or "")
            owner_kind = str(metric.get("owner_kind") or "")
            owner_name = str(metric.get("owner_name") or "")
            if namespace and replicaset and owner_kind and owner_name:
                owners[(namespace, replicaset)] = (owner_kind, owner_name)
        if owners:
            return owners
    return owners


def _resolve_controller_owners(
    pod_owners: Dict[Tuple[str, str], Tuple[str, str]],
    replicaset_owners: Dict[Tuple[str, str], Tuple[str, str]],
) -> Dict[Tuple[str, str], Tuple[str, str]]:
    resolved: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for pod_key, owner in pod_owners.items():
        namespace, _pod = pod_key
        owner_kind, owner_name = owner
        if owner_kind.lower() == "replicaset":
            resolved[pod_key] = replicaset_owners.get((namespace, owner_name), owner)
        else:
            resolved[pod_key] = owner
    return resolved


def _replica_values_by_workload(client: PrometheusClient, selector: str) -> Dict[WorkloadKey, int]:
    queries = [
        ("Deployment", "deployment", f"kube_deployment_spec_replicas{{{selector}}}"),
        ("Deployment", "deployment", f"kube_deployment_status_replicas{{{selector}}}"),
        ("StatefulSet", "statefulset", f"kube_statefulset_replicas{{{selector}}}"),
        ("StatefulSet", "statefulset", f"kube_statefulset_status_replicas{{{selector}}}"),
        ("DaemonSet", "daemonset", f"kube_daemonset_status_desired_number_scheduled{{{selector}}}"),
    ]
    out: Dict[WorkloadKey, int] = {}
    for kind, label_name, query in queries:
        try:
            rows = client.query(query)
        except Exception as exc:
            logger.warning(
                "[k8s_prometheus] 副本数查询失败，回退到指标中观测到的 Pod 数: query=%s error=%s",
                query,
                exc,
            )
            continue
        for row in rows:
            metric = row.get("metric", {})
            value = row.get("value", [])
            if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
                continue
            namespace = str(metric.get("namespace") or "")
            workload_name = str(metric.get(label_name) or "")
            try:
                replicas = int(float(value[1]))
            except Exception:
                continue
            if namespace and workload_name and replicas >= 0:
                out[(namespace, kind, workload_name)] = replicas
    return out


def _workload_key(
    key: ContainerKey,
    pod_owners: Dict[Tuple[str, str], Tuple[str, str]],
) -> Optional[WorkloadKey]:
    namespace, pod, _container = key
    owner = pod_owners.get((namespace, pod))
    if owner is None:
        return None
    owner_kind, owner_name = owner
    if not owner_kind or not owner_name:
        return None
    return (namespace, owner_kind, owner_name)


def _sum_series_by_workload(
    series_by_container: Dict[ContainerKey, pd.Series],
    cluster: str,
    pod_owners: Dict[Tuple[str, str], Tuple[str, str]],
    *,
    include_keys: Optional[Set[ContainerKey]] = None,
) -> Dict[WorkloadKey, pd.Series]:
    grouped: Dict[WorkloadKey, List[pd.Series]] = {}
    for key, series in series_by_container.items():
        if include_keys is not None and key not in include_keys:
            continue
        wk = _workload_key(key, pod_owners)
        if wk is None:
            continue
        grouped.setdefault(wk, []).append(series.astype(float))
    out: Dict[WorkloadKey, pd.Series] = {}
    for key, series_list in grouped.items():
        if not series_list:
            continue
        out[key] = pd.concat(series_list, axis=1).sum(axis=1, min_count=1).dropna().sort_index()
    return out


def _sum_series_by_workload_container(
    series_by_container: Dict[ContainerKey, pd.Series],
    pod_owners: Dict[Tuple[str, str], WorkloadKey],
) -> Dict[WorkloadKey, Dict[str, pd.Series]]:
    grouped: Dict[WorkloadKey, Dict[str, List[pd.Series]]] = {}
    for key, series in series_by_container.items():
        wk = _workload_key(key, pod_owners)
        if wk is None:
            continue
        _namespace, _pod, container = key
        if not container:
            continue
        grouped.setdefault(wk, {}).setdefault(container, []).append(series.astype(float))
    out: Dict[WorkloadKey, Dict[str, pd.Series]] = {}
    for wk, by_container in grouped.items():
        out[wk] = {}
        for container, series_list in by_container.items():
            if not series_list:
                continue
            out[wk][container] = pd.concat(series_list, axis=1).sum(axis=1, min_count=1).dropna().sort_index()
    return out


def _values_by_workload_container(
    values_by_container: Dict[ContainerKey, float],
    pod_owners: Dict[Tuple[str, str], Tuple[str, str]],
) -> Dict[WorkloadKey, Dict[str, Dict[str, Any]]]:
    grouped: Dict[WorkloadKey, Dict[str, Dict[str, Any]]] = {}
    for key, value in values_by_container.items():
        wk = _workload_key(key, pod_owners)
        if wk is None:
            continue
        namespace, pod, container = key
        if not container:
            continue
        container_values = grouped.setdefault(wk, {}).setdefault(container, {"total": 0.0, "pods": set()})
        container_values["total"] = float(container_values["total"]) + float(value)
        container_values["pods"].add((namespace, pod))
    return grouped


def _container_value(container_values: Dict[str, Dict[str, Any]], container: str) -> Optional[float]:
    item = container_values.get(container)
    if not isinstance(item, dict):
        return None
    total = item.get("total")
    pods = item.get("pods")
    if total is None or not isinstance(pods, set) or not pods:
        return None
    return float(total) / max(1, len(pods))


def _container_specs(
    containers: List[str],
    cpu_requests: Dict[str, Dict[str, Any]],
    cpu_limits: Dict[str, Dict[str, Any]],
    mem_requests: Dict[str, Dict[str, Any]],
    mem_limits: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Optional[float]]]:
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for container in containers:
        out[container] = {
            "cpu_request_cores": _container_value(cpu_requests, container),
            "cpu_limit_cores": _container_value(cpu_limits, container),
            "memory_request_gb": _bytes_to_gb(_container_value(mem_requests, container)),
            "memory_limit_gb": _bytes_to_gb(_container_value(mem_limits, container)),
        }
    return out


def _workload_metadata(
    cluster: str,
    keys: Set[ContainerKey],
    pod_owners: Dict[Tuple[str, str], Tuple[str, str]],
    cpu_usage: Dict[ContainerKey, pd.Series],
    mem_usage: Dict[ContainerKey, pd.Series],
) -> Dict[WorkloadKey, Dict[str, Set[str]]]:
    out: Dict[WorkloadKey, Dict[str, Set[str]]] = {}
    for key in keys:
        namespace, pod, container = key
        wk = _workload_key(key, pod_owners)
        if wk is None:
            continue
        meta = out.setdefault(wk, {"pods": set(), "containers": set(), "nodes": set()})
        if pod:
            meta["pods"].add(pod)
        if container:
            meta["containers"].add(container)
        node = _last_label(cpu_usage.get(key), "node") or _last_label(mem_usage.get(key), "node")
        if node:
            meta["nodes"].add(node)
    return out


def _regularize_series(
    series: pd.Series,
    step_seconds: int,
    max_gap_steps: int,
) -> pd.Series:
    ordered = series.sort_index()
    if ordered.empty:
        return ordered
    rule = f"{max(1, int(step_seconds))}s"
    resampled = ordered.resample(rule).mean()
    missing = resampled.isna()
    if not missing.any() or int(max_gap_steps) <= 0:
        return resampled.dropna()

    # Fill only complete bounded gaps. Use past values rather than two-sided
    # interpolation: preprocessing happens before temporal evaluation splits.
    run_ids = missing.ne(missing.shift(fill_value=False)).cumsum()
    run_lengths = missing.groupby(run_ids).transform("sum")
    bounded_missing = missing & (run_lengths <= max(0, int(max_gap_steps)))
    filled = resampled.copy()
    filled.loc[bounded_missing] = resampled.ffill().loc[bounded_missing]
    return filled.dropna()


def _bytes_to_gb(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(value) / BYTES_PER_GIB


def _series_payload(s: pd.Series) -> Dict[str, List[float]]:
    idx = s.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(None)
    return {
        "timestamps": (idx.view("int64") // 1_000_000).astype(int).tolist(),
        "values": s.to_numpy(dtype=float).tolist(),
    }


def _data_quality(s: pd.Series, step_seconds: int) -> Dict[str, Any]:
    arr = s.dropna()
    expected = 0
    if not arr.empty:
        span = max(0.0, (arr.index[-1] - arr.index[0]).total_seconds())
        expected = int(span // max(1, step_seconds)) + 1
    missing_ratio = 1.0 - (len(arr) / expected) if expected > 0 else 1.0
    diffs = np.diff(arr.index.view("int64") // 1_000_000_000) if len(arr) >= 2 else np.array([])
    max_gap = int(np.max(diffs)) if diffs.size else 0
    if len(arr) < 24 or missing_ratio > 0.35 or max_gap > step_seconds * 12:
        level = "poor"
    elif missing_ratio > 0.12 or max_gap > step_seconds * 4:
        level = "fair"
    else:
        level = "good"
    return {
        "level": level,
        "points": int(len(arr)),
        "expected_points": int(expected),
        "missing_ratio": round(float(max(0.0, missing_ratio)), 4),
        "max_gap_seconds": max_gap,
    }


def _last_label(s: Optional[pd.Series], name: str) -> str:
    labels = getattr(s, "attrs", {}).get("labels", {}) if s is not None else {}
    return str(labels.get(name) or "") if isinstance(labels, dict) else ""
