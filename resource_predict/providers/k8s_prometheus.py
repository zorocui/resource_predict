from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import numpy as np
import pandas as pd

from resource_predict.settings import settings
from resource_predict.services.cluster_configs import (
    K8S_PROMETHEUS_CONFIG_PATH,
    ClusterConfigValidationError,
    read_k8s_prometheus_clusters,
)


@dataclass(frozen=True)
class PrometheusClient:
    base_url: str
    bearer_token: str = ""
    basic_auth: str = ""
    timeout_seconds: int = 30

    def query(self, query: str, *, ts: Optional[float] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"query": query}
        if ts is not None:
            params["time"] = ts
        return self._get("/api/v1/query", params).get("result", [])

    def query_range(self, query: str, *, start: float, end: float, step: int) -> List[Dict[str, Any]]:
        return self._get(
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        ).get("result", [])

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        if self.bearer_token:
            req.add_header("Authorization", f"Bearer {self.bearer_token}")
        if self.basic_auth:
            req.add_header("Authorization", f"Basic {self.basic_auth}")
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
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
    rate_window: str = "5m"


ContainerKey = Tuple[str, str, str]
WorkloadKey = Tuple[str, str, str]


def k8s_workload_prometheus_provider(
    *,
    resources: int,
    n: int,
    freq: str,
    clusters: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch K8S controller-level workload resources from Prometheus.

    ``resources <= 0`` means unlimited, which is useful for production upsert.
    The ``n`` and ``freq`` arguments are accepted for compatibility with the
    generic data provider interface.
    """
    cfg = settings.k8s_prometheus
    targets = _resolve_targets()
    wanted = {str(x).strip() for x in clusters or [] if str(x).strip()}
    if wanted:
        targets = [target for target in targets if target.cluster in wanted]
        missing = wanted - {target.cluster for target in targets}
        if missing:
            raise ValueError(f"未找到 K8S Prometheus 集群配置: {', '.join(sorted(missing))}")
    if not targets:
        raise ValueError(
            "请配置 settings.k8s_prometheus.clusters，"
            "或环境变量 K8S_PROMETHEUS_CLUSTERS"
        )

    limit = int(resources or 0)
    out: List[Dict[str, Any]] = []
    errors: List[str] = []
    for target in targets:
        try:
            remaining = 0 if limit <= 0 else max(0, limit - len(out))
            if limit > 0 and remaining <= 0:
                break
            out.extend(_fetch_target(target, remaining))
        except Exception as exc:
            msg = f"{target.cluster}({target.prometheus_url}): {exc}"
            errors.append(msg)
            if cfg.fail_fast:
                raise RuntimeError(f"K8S Prometheus 拉取失败: {msg}") from exc

    if not out and errors:
        raise RuntimeError("所有 K8S Prometheus 集群拉取失败: " + "；".join(errors))
    return out


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

        pod_owners_raw = _pod_owner_values(client, owner_selector)
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
            f'kube_pod_container_resource_requests{{{selector},resource="cpu"}}',
        ])
        cpu_limit = _instant_values(client, [
            f"kube_pod_container_resource_limits_cpu_cores{{{selector}}}",
            f'kube_pod_container_resource_limits{{{selector},resource="cpu"}}',
        ])
        mem_request = _instant_values(client, [
            f"kube_pod_container_resource_requests_memory_bytes{{{selector}}}",
            f'kube_pod_container_resource_requests{{{selector},resource="memory"}}',
        ])
        mem_limit = _instant_values(client, [
            f"kube_pod_container_resource_limits_memory_bytes{{{selector}}}",
            f'kube_pod_container_resource_limits{{{selector},resource="memory"}}',
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


def _fetch_target(target: PrometheusTarget, limit: int) -> List[Dict[str, Any]]:
    client = PrometheusClient(
        base_url=target.prometheus_url,
        bearer_token=target.bearer_token,
        basic_auth=target.basic_auth,
        timeout_seconds=int(target.request_timeout_seconds),
    )
    end = time.time()
    start = end - int(target.history_days) * 86400
    step = int(target.step_seconds)
    selector = 'container!="",container!="POD",pod!=""'
    if target.namespace_regex:
        selector += f',namespace=~"{target.namespace_regex}"'
    owner_selector = 'pod!=""'
    replicaset_owner_selector = ""
    if target.namespace_regex:
        owner_selector += f',namespace=~"{target.namespace_regex}"'
        replicaset_owner_selector = f'namespace=~"{target.namespace_regex}"'

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
        f'kube_pod_container_resource_requests{{{selector},resource="cpu"}}',
    ])
    cpu_limit = _instant_values(client, [
        f"kube_pod_container_resource_limits_cpu_cores{{{selector}}}",
        f'kube_pod_container_resource_limits{{{selector},resource="cpu"}}',
    ])
    mem_request = _instant_values(client, [
        f"kube_pod_container_resource_requests_memory_bytes{{{selector}}}",
        f'kube_pod_container_resource_requests{{{selector},resource="memory"}}',
    ])
    mem_limit = _instant_values(client, [
        f"kube_pod_container_resource_limits_memory_bytes{{{selector}}}",
        f'kube_pod_container_resource_limits{{{selector},resource="memory"}}',
    ])
    pod_owners = _pod_owner_values(client, owner_selector)
    replicaset_owners = _replicaset_owner_values(client, replicaset_owner_selector)
    if replicaset_owners:
        pod_owners = _resolve_controller_owners(pod_owners, replicaset_owners)
    replica_values = _replica_values_by_workload(client, replicaset_owner_selector)

    workload_keys = sorted(
        wk
        for wk in {
            _workload_key(key, pod_owners)
            for key in set(cpu_usage) | set(mem_usage)
        }
        if wk is not None
    )
    cpu_usage_by_workload = _sum_series_by_workload(cpu_usage, target.cluster, pod_owners)
    mem_usage_by_workload = _sum_series_by_workload(mem_usage, target.cluster, pod_owners)
    cpu_request_by_workload, cpu_request_pod_counts = _sum_values_by_workload(cpu_request, target.cluster, pod_owners)
    cpu_limit_by_workload, cpu_limit_pod_counts = _sum_values_by_workload(cpu_limit, target.cluster, pod_owners)
    mem_request_by_workload, mem_request_pod_counts = _sum_values_by_workload(mem_request, target.cluster, pod_owners)
    mem_limit_by_workload, mem_limit_pod_counts = _sum_values_by_workload(mem_limit, target.cluster, pod_owners)
    metadata_by_workload = _workload_metadata(
        target.cluster,
        set(cpu_usage) | set(mem_usage),
        pod_owners,
        cpu_usage,
        mem_usage,
    )

    out: List[Dict[str, Any]] = []
    for key in workload_keys:
        cpu_s = cpu_usage_by_workload.get(key)
        mem_s = mem_usage_by_workload.get(key)
        if cpu_s is None or mem_s is None:
            continue
        cpu_base, cpu_base_name = _select_denominator(
            (cpu_limit_by_workload.get(key), "cpu_usage/cpu_limit"),
            (cpu_request_by_workload.get(key), "cpu_usage/cpu_request"),
        )
        mem_base, mem_base_name = _select_denominator(
            (mem_limit_by_workload.get(key), "memory_working_set/memory_limit"),
            (mem_request_by_workload.get(key), "memory_working_set/memory_request"),
        )
        cpu_metric, cpu_norm = _normalize_series(cpu_s, cpu_base, cpu_base_name, "cpu_usage_cores")
        mem_metric, mem_norm = _normalize_series(
            mem_s / (1024 ** 3),
            (mem_base / (1024 ** 3)) if mem_base else None,
            mem_base_name,
            "memory_working_set_gb",
        )
        cpu_quality = _data_quality(cpu_norm, step)
        mem_quality = _data_quality(mem_norm, step)
        cpu_norm = _regularize_series(cpu_norm, step)
        mem_norm = _regularize_series(mem_norm, step)
        namespace, owner_kind, owner_name = key
        meta = metadata_by_workload.get(key, {})
        # 优先使用 kube-state-metrics 上报的控制器副本数（spec/status replicas），
        # 它来自 K8s API 是权威值；仅当 kube-state-metrics 无数据时才回退到
        # Prometheus 中有容器指标的 pod 数，避免某个 pod 未上报指标时低估副本数。
        kube_replicas = replica_values.get(key)
        if kube_replicas is not None and kube_replicas > 0:
            replicas_observed = kube_replicas
        else:
            replicas_observed = len(meta.get("pods", []))
        cpu_request_total = cpu_request_by_workload.get(key)
        cpu_limit_total = cpu_limit_by_workload.get(key)
        mem_request_total = mem_request_by_workload.get(key)
        mem_limit_total = mem_limit_by_workload.get(key)
        # 用实际有该指标的 pod 数做除数，避免未上报指标的 pod 拉低单 pod 均值
        cpu_request_pod_ct = cpu_request_pod_counts.get(key, replicas_observed)
        cpu_limit_pod_ct = cpu_limit_pod_counts.get(key, replicas_observed)
        mem_request_pod_ct = mem_request_pod_counts.get(key, replicas_observed)
        mem_limit_pod_ct = mem_limit_pod_counts.get(key, replicas_observed)
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
            "cpu_request_cores": _per_pod_value(cpu_request_total, cpu_request_pod_ct),
            "cpu_limit_cores": _per_pod_value(cpu_limit_total, cpu_limit_pod_ct),
            "memory_request_gb": _bytes_to_gb(_per_pod_value(mem_request_total, mem_request_pod_ct)),
            "memory_limit_gb": _bytes_to_gb(_per_pod_value(mem_limit_total, mem_limit_pod_ct)),
            "cpu_request_cores_total": cpu_request_total,
            "cpu_limit_cores_total": cpu_limit_total,
            "memory_request_gb_total": _bytes_to_gb(mem_request_total),
            "memory_limit_gb_total": _bytes_to_gb(mem_limit_total),
            "cpu_metric_mode": cpu_metric,
            "memory_metric_mode": mem_metric,
        }
        nodes = sorted(meta.get("nodes", []))
        if nodes:
            spec["nodes"] = nodes
        item = {
            "resource_id": f"k8s:{target.cluster}:{namespace}:{owner_kind.lower()}:{owner_name}",
            "resource_type": "k8s_workload",
            "spec": spec,
            "metrics": {
                "cpu": _series_payload(cpu_norm),
                "memory": _series_payload(mem_norm),
            },
            "data_quality": {
                "cpu": cpu_quality,
                "memory": mem_quality,
            },
        }
        out.append(item)
        if limit > 0 and len(out) >= limit:
            break
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
                rate_window=str(data.get("rate_window") or cfg.rate_window or "5m").strip(),
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
        except Exception:
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


def _pod_owner_values(client: PrometheusClient, selector: str) -> Dict[Tuple[str, str], Tuple[str, str]]:
    queries = [
        f'kube_pod_owner{{{selector},owner_is_controller="true"}}',
        f"kube_pod_owner{{{selector}}}",
    ]
    owners: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for query in queries:
        try:
            rows = client.query(query)
        except Exception:
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
        except Exception:
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
        except Exception:
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
) -> Dict[WorkloadKey, pd.Series]:
    grouped: Dict[WorkloadKey, List[pd.Series]] = {}
    for key, series in series_by_container.items():
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


def _sum_values_by_workload(
    values_by_container: Dict[ContainerKey, float],
    cluster: str,
    pod_owners: Dict[Tuple[str, str], Tuple[str, str]],
) -> Tuple[Dict[WorkloadKey, float], Dict[WorkloadKey, int]]:
    out: Dict[WorkloadKey, float] = {}
    pod_counts: Dict[WorkloadKey, set] = {}
    for key, value in values_by_container.items():
        wk = _workload_key(key, pod_owners)
        if wk is None:
            continue
        out[wk] = out.get(wk, 0.0) + float(value)
        # 记录有该指标的 pod（namespace + pod_name），用于后续按 pod 数求均值
        namespace, pod, _container = key
        pod_counts.setdefault(wk, set()).add((namespace, pod))
    pod_counts_int: Dict[WorkloadKey, int] = {
        wk: len(pods) for wk, pods in pod_counts.items()
    }
    return out, pod_counts_int


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


def _select_denominator(*candidates: Tuple[Optional[float], str]) -> Tuple[Optional[float], str]:
    for value, name in candidates:
        if value and value > 0:
            return float(value), name
    return None, ""


def _per_pod_value(value: Optional[float], replicas: int) -> Optional[float]:
    if value is None:
        return None
    divisor = max(1, int(replicas or 0))
    return float(value) / divisor


def _normalize_series(
    series: pd.Series,
    denominator: Optional[float],
    ratio_name: str,
    raw_name: str,
) -> Tuple[str, pd.Series]:
    if denominator and denominator > 0:
        return ratio_name, series.astype(float) / float(denominator)
    return raw_name, series.astype(float)


def _regularize_series(series: pd.Series, step_seconds: int) -> pd.Series:
    s = series.sort_index()
    if s.empty:
        return s
    rule = f"{max(1, int(step_seconds))}s"
    out = s.resample(rule).mean()
    if out.isna().any():
        out = out.interpolate(method="time").ffill().bfill()
    return out


def _bytes_to_gb(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(value) / (1024 ** 3)


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
