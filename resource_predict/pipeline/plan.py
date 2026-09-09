from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

from resource_predict.pipeline.constants import METRIC_NAMES


def normalize_metric_filter(
    metric_names_by_resource: Optional[Dict[str, Any]],
) -> Dict[str, Set[str]]:
    if not isinstance(metric_names_by_resource, dict):
        return {}
    out: Dict[str, Set[str]] = {}
    allowed = set(METRIC_NAMES)
    for rid, names in metric_names_by_resource.items():
        if names is None:
            continue
        if isinstance(names, str):
            raw_names = [names]
        else:
            try:
                raw_names = list(names)
            except TypeError:
                continue
        clean = {str(x).strip().lower() for x in raw_names}
        clean = {x for x in clean if x in allowed}
        if clean:
            out[str(rid)] = clean
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cgroup_cpu_limits() -> list[int]:
    """Read root and current-group quotas, including limits inherited from parents."""
    root = Path("/sys/fs/cgroup")
    locations = {(root, root), (root / "cpu", root / "cpu"),
                 (root / "cpu,cpuacct", root / "cpu,cpuacct")}
    for line in _read_text(Path("/proc/self/cgroup")).splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, relative = parts
        if ".." in Path(relative).parts:
            continue
        if not controllers:
            locations.add((root / relative.lstrip("/"), root))
        elif "cpu" in controllers.split(","):
            for base in (root / "cpu", root / "cpu,cpuacct"):
                locations.add((base / relative.lstrip("/"), base))
    limits = []
    for location, base in locations:
        while location.is_relative_to(base):
            values = _read_text(location / "cpu.max").split()
            if len(values) != 2:
                values = [_read_text(location / "cpu.cfs_quota_us"),
                          _read_text(location / "cpu.cfs_period_us")]
            try:
                quota, period = map(int, values)
                if quota > 0 and period > 0:
                    limits.append(max(1, quota // period))
            except ValueError:
                pass
            if location == base:
                break
            location = location.parent
    return limits


def available_cpu_count() -> int:
    counts = [max(1, os.cpu_count() or 1)]
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            counts.append(max(1, len(affinity(0))))
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        counts.extend(_cgroup_cpu_limits())
    return min(counts)


def resolve_execution_plan(task_count: int, active_methods: list[str], *,
                           backend: str = "auto", max_workers: int = 0) -> dict:
    if isinstance(task_count, bool) or not isinstance(task_count, int) or task_count < 0:
        raise ValueError("task_count must be a non-negative integer")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 0:
        raise ValueError("max_workers must be a non-negative integer (0 means auto)")
    if backend not in ("auto", "serial", "thread", "process"):
        raise ValueError("parallel backend must be auto, serial, thread or process")
    cpu = available_cpu_count()
    if backend == "auto":
        backend = "process" if set(active_methods) & {"arima", "sarima", "prophet"} else "thread"
    workers = max(1, min(task_count, cpu, max_workers or max(1, cpu - 1)))
    if backend == "process" and sys.platform == "win32":
        workers = min(workers, 61)
    if backend == "serial" or workers == 1:
        backend, workers = "serial", 1
    return {"backend": backend, "workers": workers, "available_cpus": cpu,
            "start_method": "spawn" if backend == "process" else None,
            "max_in_flight": 2 * workers, "task_count": task_count}
