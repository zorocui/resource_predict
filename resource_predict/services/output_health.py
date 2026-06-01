from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from resource_predict.pipeline.constants import DETAILS_DIRNAME, RAW_DATA_FILENAME, SUMMARY_INDEX_FILENAME
from resource_predict.pipeline.output_paths import all_scoped_out_dirs


VM_METRICS = ("cpu", "memory", "disk")
K8S_WORKLOAD_METRICS = ("cpu_limit", "cpu_request", "memory_limit", "memory_request")


def check_outputs(out_dir: Path | str, *, require_both_types: bool = True) -> Dict[str, Any]:
    base = Path(out_dir)
    scoped_dirs = [
        (scope, path)
        for scope, path in all_scoped_out_dirs(base)
        if (path / SUMMARY_INDEX_FILENAME).exists() or (path / RAW_DATA_FILENAME).exists()
    ]
    if not scoped_dirs:
        return _report(
            False,
            [f"缺少 scoped 输出目录: {base / 'vm'} 和 {base / 'k8s'}"],
            [],
            {},
            {},
            [],
        )

    errors: List[str] = []
    warnings: List[str] = []
    summary_counts: Counter[str] = Counter()
    raw_counts: Counter[str] = Counter()
    samples: List[Dict[str, Any]] = []
    for scope, path in scoped_dirs:
        report = _check_single_output(path, require_both_types=False)
        errors.extend(f"{scope}: {error}" for error in report.get("errors", []))
        warnings.extend(f"{scope}: {warning}" for warning in report.get("warnings", []))
        summary_counts.update(report.get("summary_counts", {}))
        raw_counts.update(report.get("raw_counts", {}))
        samples.extend(report.get("sample_workloads", []))

    if require_both_types:
        _require_type(summary_counts, "openstack_vm", "summary_index", errors)
        _require_type(summary_counts, "k8s_workload", "summary_index", errors)
        _require_type(raw_counts, "openstack_vm", "raw_data", errors)
        _require_type(raw_counts, "k8s_workload", "raw_data", errors)

    return _report(
        not errors,
        errors,
        warnings,
        dict(sorted(summary_counts.items())),
        dict(sorted(raw_counts.items())),
        samples[:5],
    )


def _check_single_output(out_dir: Path | str, *, require_both_types: bool = True) -> Dict[str, Any]:
    """Validate generated prediction artifacts against the VM + K8S Workload contract."""
    base = Path(out_dir)
    errors: List[str] = []
    warnings: List[str] = []

    summary_obj = _read_json(base / SUMMARY_INDEX_FILENAME, errors)
    raw_obj = _read_json(base / RAW_DATA_FILENAME, errors)
    if summary_obj is None or raw_obj is None:
        return _report(False, errors, warnings, {}, {}, [])

    summary_resources = _resources(summary_obj, SUMMARY_INDEX_FILENAME, errors)
    raw_resources = _resources(raw_obj, RAW_DATA_FILENAME, errors)
    if summary_resources is None or raw_resources is None:
        return _report(False, errors, warnings, {}, {}, [])

    summary_by_type = Counter(_resource_type(item) for item in summary_resources)
    raw_by_type = Counter(_resource_type(item) for item in raw_resources)
    raw_by_id = {str(item.get("resource_id")): item for item in raw_resources if isinstance(item, dict)}

    if require_both_types:
        _require_type(summary_by_type, "openstack_vm", "summary_index", errors)
        _require_type(summary_by_type, "k8s_workload", "summary_index", errors)
        _require_type(raw_by_type, "openstack_vm", "raw_data", errors)
        _require_type(raw_by_type, "k8s_workload", "raw_data", errors)

    _reject_unknown_types(summary_resources, "summary_index", errors)
    _reject_unknown_types(raw_resources, "raw_data", errors)

    details_cache = _load_detail_chunks(base, summary_obj, errors, warnings)
    detail_items = _validate_detail_refs(summary_resources, details_cache, errors)
    _reject_unknown_types(detail_items, "details", errors)
    _validate_detail_contracts(detail_items, errors)

    for item in summary_resources:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("resource_id") or "")
        raw = raw_by_id.get(rid)
        if raw is None:
            errors.append(f"{rid}: summary_index 中存在，但 raw_data 中缺失")
            continue
        rtype = _resource_type(item)
        if rtype == "openstack_vm":
            _check_vm_summary(item, raw, errors, warnings)
        elif rtype == "k8s_workload":
            _check_k8s_workload_summary(item, raw, errors, warnings)

    samples = [
        {
            "resource_id": str(item.get("resource_id")),
            "namespace": (item.get("spec") or {}).get("namespace"),
            "workload_kind": (item.get("spec") or {}).get("workload_kind"),
            "workload_name": (item.get("spec") or {}).get("workload_name"),
        }
        for item in summary_resources
        if isinstance(item, dict) and _resource_type(item) == "k8s_workload"
    ][:5]

    return _report(
        not errors,
        errors,
        warnings,
        dict(sorted(summary_by_type.items())),
        dict(sorted(raw_by_type.items())),
        samples,
    )


def format_health_report(report: Dict[str, Any]) -> str:
    status = "OK" if report.get("ok") else "FAILED"
    lines = [f"Output health: {status}"]
    lines.append(f"Summary counts: {_format_counts(report.get('summary_counts', {}))}")
    lines.append(f"Raw counts: {_format_counts(report.get('raw_counts', {}))}")

    samples = report.get("sample_workloads") or []
    if samples:
        rendered = ", ".join(
            f"{x.get('namespace')}/{x.get('workload_kind')}/{x.get('workload_name')}"
            for x in samples
            if isinstance(x, dict)
        )
        lines.append(f"Sample workloads: {rendered}")

    warnings = report.get("warnings") or []
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)

    errors = report.get("errors") or []
    if errors:
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in errors)

    return "\n".join(lines)


def _read_json(path: Path, errors: List[str]) -> Dict[str, Any] | None:
    if not path.exists():
        errors.append(f"缺少文件: {path}")
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"{path}: JSON 读取失败: {exc}")
        return None
    if not isinstance(obj, dict):
        errors.append(f"{path}: 根节点必须是 object")
        return None
    return obj


def _resources(obj: Dict[str, Any], label: str, errors: List[str]) -> List[Dict[str, Any]] | None:
    resources = obj.get("resources")
    if not isinstance(resources, list) or not resources:
        errors.append(f"{label}: resources 必须是非空 list")
        return None
    return resources


def _resource_type(item: Dict[str, Any]) -> str:
    raw = str(item.get("resource_type") or "").strip().lower().replace("-", "_")
    return raw or "openstack_vm"


def _require_type(counts: Counter[str], resource_type: str, label: str, errors: List[str]) -> None:
    if counts.get(resource_type, 0) <= 0:
        errors.append(f"{label}: 缺少 resource_type={resource_type} 的资源")


def _reject_unknown_types(items: Iterable[Dict[str, Any]], label: str, errors: List[str]) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        rtype = _resource_type(item)
        if rtype not in {"openstack_vm", "k8s_workload"}:
            errors.append(f"{label}: 不支持的资源类型 {rtype}（资源 {item.get('resource_id')}）")


def _load_detail_chunks(
    base: Path,
    summary_obj: Dict[str, Any],
    errors: List[str],
    warnings: List[str],
) -> Dict[str, Dict[str, Any]]:
    meta = summary_obj.get("meta") if isinstance(summary_obj.get("meta"), dict) else {}
    files = meta.get("details_files")
    if files is None:
        warnings.append("summary_index.meta.details_files 缺失，将只按 detail_ref 懒加载")
        files = []
    if not isinstance(files, list):
        errors.append("summary_index.meta.details_files 必须是 list")
        return {}

    details_dir = str(meta.get("details_dir") or DETAILS_DIRNAME)
    loaded: Dict[str, Dict[str, Any]] = {}
    for file_name in files:
        name = str(file_name)
        obj = _read_json(base / details_dir / name, errors)
        if obj is not None:
            loaded[name] = obj
    return loaded


def _validate_detail_refs(
    summary_resources: List[Dict[str, Any]],
    details_cache: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> List[Dict[str, Any]]:
    detail_items: List[Dict[str, Any]] = []
    for item in summary_resources:
        if not isinstance(item, dict):
            errors.append("summary_index: resources 中存在非 object 项")
            continue
        rid = str(item.get("resource_id") or "")
        ref = item.get("detail_ref")
        if not isinstance(ref, dict):
            errors.append(f"{rid}: detail_ref 必须是 object")
            continue
        file_name = str(ref.get("file") or "")
        if not file_name:
            errors.append(f"{rid}: detail_ref.file 缺失")
            continue
        chunk = details_cache.get(file_name)
        if chunk is None:
            errors.append(f"{rid}: detail_ref.file 指向不存在的详情分片 {file_name}")
            continue
        resources = chunk.get("resources")
        if not isinstance(resources, list):
            errors.append(f"{file_name}: resources 必须是 list")
            continue
        try:
            offset = int(ref.get("offset"))
        except (TypeError, ValueError):
            errors.append(f"{rid}: detail_ref.offset 非法")
            continue
        if offset < 0 or offset >= len(resources):
            errors.append(f"{rid}: detail_ref.offset 越界")
            continue
        detail = resources[offset]
        if not isinstance(detail, dict):
            errors.append(f"{rid}: detail_ref 指向的详情项不是 object")
            continue
        if str(detail.get("resource_id") or "") != rid:
            errors.append(f"{rid}: detail_ref 指向资源 {detail.get('resource_id')}，不一致")
            continue
        detail_items.append(detail)
    return detail_items


def _check_vm_summary(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    errors: List[str],
    warnings: List[str],
) -> None:
    rid = str(item.get("resource_id") or "")
    _check_metrics(raw, VM_METRICS, f"{rid} raw_data", errors)
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    for key in ("cpu_cores", "memory_gb", "disk_gb"):
        if key not in spec:
            warnings.append(f"{rid}: VM spec 缺少 {key}")
    advice = item.get("scaling_advice")
    if not isinstance(advice, dict):
        errors.append(f"{rid}: scaling_advice 必须是 object")
        return
    if not str(advice.get("action") or ""):
        errors.append(f"{rid}: scaling_advice.action 缺失")


def _check_k8s_workload_summary(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    errors: List[str],
    warnings: List[str],
) -> None:
    rid = str(item.get("resource_id") or "")
    parts = rid.split(":")
    if len(parts) != 5 or parts[0] != "k8s":
        errors.append(f"{rid}: K8S Workload ID 必须形如 k8s:cluster:namespace:kind:name")

    _check_metrics(raw, K8S_WORKLOAD_METRICS, f"{rid} raw_data", errors)

    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    for key in ("cluster", "namespace", "workload_kind", "workload_name"):
        if not str(spec.get(key) or ""):
            errors.append(f"{rid}: Workload spec 缺少 {key}")
    _check_non_empty_list(spec, "pods_observed", rid, errors)
    _check_non_empty_list(spec, "containers_observed", rid, errors)
    _check_container_specs(spec, rid, errors)

    replicas = spec.get("replicas_observed")
    if not isinstance(replicas, int) or replicas <= 0:
        errors.append(f"{rid}: replicas_observed 必须是正整数")

    advice = item.get("scaling_advice")
    if not isinstance(advice, dict):
        errors.append(f"{rid}: scaling_advice 必须是 object")
        return
    if _resource_type(advice) != "k8s_workload":
        errors.append(f"{rid}: scaling_advice.resource_type 必须是 k8s_workload")
    if not isinstance(advice.get("target_k8s_policy"), dict):
        errors.append(f"{rid}: target_k8s_policy 缺失")
    _check_k8s_target_contract(rid, advice, errors)
    if str(advice.get("action") or "") not in {
        "scale_out_candidate",
        "scale_in_candidate",
        "hold",
        "insufficient_data",
    }:
        warnings.append(f"{rid}: K8S action 不在标准集合中: {advice.get('action')}")


def _validate_detail_contracts(detail_items: List[Dict[str, Any]], errors: List[str]) -> None:
    for item in detail_items:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("resource_id") or "")
        if _resource_type(item) != "k8s_workload":
            continue

        charts = item.get("charts_forecast")
        if not isinstance(charts, dict):
            errors.append(f"{rid}: details 缺少 charts_forecast")
        else:
            for metric in K8S_WORKLOAD_METRICS:
                if not isinstance(charts.get(metric), dict):
                    errors.append(f"{rid}: details.charts_forecast 缺少 {metric}")

        advice = item.get("scaling_advice")
        if not isinstance(advice, dict):
            errors.append(f"{rid}: details.scaling_advice 必须是 object")
            continue
        if not isinstance(advice.get("target_k8s_policy"), dict):
            errors.append(f"{rid}: details.target_k8s_policy 缺失")
        _check_k8s_target_contract(rid, advice, errors, prefix="details.")


def _check_k8s_target_contract(
    rid: str,
    advice: Dict[str, Any],
    errors: List[str],
    *,
    prefix: str = "",
) -> None:
    if advice.get("analysis_only") is True:
        return
    action = str(advice.get("action") or "")
    if action not in {"scale_out_candidate", "scale_in_candidate"}:
        return
    target = advice.get("target_spec")
    if not isinstance(target, dict) or not target:
        errors.append(f"{rid}: {prefix}K8S executable advice must include target_spec")
        return
    has_resource_target = any(
        key in target
        for key in (
            "cpu_request_cores",
            "cpu_limit_cores",
            "memory_request_gb",
            "memory_limit_gb",
            "cpu_cores",
            "memory_gb",
        )
    )
    has_replica_target = target.get("replicas") is not None
    if not has_resource_target and not has_replica_target:
        errors.append(f"{rid}: {prefix}K8S target_spec must include resources or replicas")


def _check_metrics(
    item: Dict[str, Any],
    expected_metrics: Tuple[str, ...],
    label: str,
    errors: List[str],
) -> None:
    metrics = item.get("metrics")
    if not isinstance(metrics, dict):
        errors.append(f"{label}: metrics 必须是 object")
        return
    for name in expected_metrics:
        block = metrics.get(name)
        if not isinstance(block, dict):
            errors.append(f"{label}: 缺少 {name} 指标")
            continue
        timestamps = block.get("timestamps")
        values = block.get("values")
        if not isinstance(timestamps, list) or not isinstance(values, list) or not timestamps or not values:
            errors.append(f"{label}: {name} 指标 timestamps/values 必须是非空 list")
        elif len(timestamps) != len(values):
            errors.append(f"{label}: {name} 指标 timestamps/values 长度不一致")


def _check_non_empty_list(spec: Dict[str, Any], key: str, rid: str, errors: List[str]) -> None:
    value = spec.get(key)
    if not isinstance(value, list) or not value:
        errors.append(f"{rid}: Workload spec 缺少非空 {key}")


def _check_container_specs(spec: Dict[str, Any], rid: str, errors: List[str]) -> None:
    containers = spec.get("containers")
    observed = spec.get("containers_observed")
    if not isinstance(containers, dict) or not containers:
        errors.append(f"{rid}: Workload spec 缺少非空 containers")
        return
    if isinstance(observed, list):
        missing = [str(name) for name in observed if str(name or "") not in containers]
        if missing:
            errors.append(f"{rid}: Workload spec.containers 缺少 {','.join(missing)}")


def _format_counts(counts: Dict[str, int]) -> str:
    if not counts:
        return "{}"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _report(
    ok: bool,
    errors: List[str],
    warnings: List[str],
    summary_counts: Dict[str, int],
    raw_counts: Dict[str, int],
    sample_workloads: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "ok": ok,
        "summary_counts": summary_counts,
        "raw_counts": raw_counts,
        "sample_workloads": sample_workloads,
        "warnings": warnings,
        "errors": errors,
    }
