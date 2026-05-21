from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from resource_predict.settings import settings


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


def k8s_pod_prometheus_provider(*, resources: int, n: int, freq: str) -> List[Dict[str, Any]]:
    cfg = settings.k8s_prometheus
    base_url = os.getenv("K8S_PROMETHEUS_URL") or cfg.prometheus_url
    if not base_url:
        raise ValueError("请配置 settings.k8s_prometheus.prometheus_url 或环境变量 K8S_PROMETHEUS_URL")
    client = PrometheusClient(
        base_url=base_url,
        bearer_token=os.getenv("K8S_PROMETHEUS_BEARER_TOKEN", ""),
        basic_auth=os.getenv("K8S_PROMETHEUS_BASIC_AUTH", ""),
        timeout_seconds=int(cfg.request_timeout_seconds),
    )
    end = time.time()
    start = end - int(cfg.history_days) * 86400
    step = int(cfg.step_seconds)
    selector = 'container!="",pod!=""'
    if cfg.namespace_regex:
        selector += f',namespace=~"{cfg.namespace_regex}"'

    cpu_usage = _range_by_key(
        client.query_range(
            f"rate(container_cpu_usage_seconds_total{{{selector}}}[5m])",
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

    out: List[Dict[str, Any]] = []
    for key in sorted(set(cpu_usage) | set(mem_usage)):
        cpu_s = cpu_usage.get(key)
        mem_s = mem_usage.get(key)
        if cpu_s is None or mem_s is None:
            continue
        cpu_base = cpu_request.get(key) or cpu_limit.get(key)
        mem_base = mem_limit.get(key) or mem_request.get(key)
        cpu_metric, cpu_norm = _normalize_series(cpu_s, cpu_base)
        mem_metric, mem_norm = _normalize_series(mem_s / (1024 ** 3), (mem_base / (1024 ** 3)) if mem_base else None)
        cpu_quality = _data_quality(cpu_norm, step)
        mem_quality = _data_quality(mem_norm, step)
        cpu_norm = _regularize_series(cpu_norm, step)
        mem_norm = _regularize_series(mem_norm, step)
        namespace, pod, container = key
        spec = {
            "cluster": cfg.cluster,
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "cpu_request_cores": cpu_request.get(key),
            "cpu_limit_cores": cpu_limit.get(key),
            "memory_request_gb": _bytes_to_gb(mem_request.get(key)),
            "memory_limit_gb": _bytes_to_gb(mem_limit.get(key)),
            "cpu_metric_mode": cpu_metric,
            "memory_metric_mode": mem_metric,
        }
        node = _last_label(cpu_s, "node") or _last_label(mem_s, "node")
        if node:
            spec["node"] = node
        item = {
            "resource_id": f"k8s:{cfg.cluster}:{namespace}:{pod}:{container}",
            "resource_type": "k8s_pod",
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
        if len(out) >= resources:
            break
    return out


def _key(metric: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(metric.get("namespace") or ""),
        str(metric.get("pod") or ""),
        str(metric.get("container") or ""),
    )


def _range_by_key(result: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str, str], pd.Series]:
    out: Dict[Tuple[str, str, str], pd.Series] = {}
    labels_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
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
        labels_by_key[key] = metric if isinstance(metric, dict) else {}
    return out


def _instant_values(client: PrometheusClient, queries: List[str]) -> Dict[Tuple[str, str, str], float]:
    merged: Dict[Tuple[str, str, str], float] = {}
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


def _normalize_series(series: pd.Series, denominator: Optional[float]) -> Tuple[str, pd.Series]:
    if denominator and denominator > 0:
        return "ratio", series.astype(float) / float(denominator)
    return "raw", series.astype(float)


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
    diffs = np.diff(arr.index.view("int64") // 1_000_000) if len(arr) >= 2 else np.array([])
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

