from __future__ import annotations

import os
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


def resolve_parallel_plan(
    *,
    resources_ct: int,
    cfg: Any,
    max_workers: Optional[int],
) -> tuple[int, bool, int]:
    cpu = max(1, os.cpu_count() or 4)
    if max_workers is not None:
        outer_workers = max(1, min(resources_ct, int(max_workers)))
        inner_enabled = outer_workers == 1
    else:
        configured = getattr(cfg, "max_workers", None)
        if configured is not None:
            outer_workers = max(1, min(resources_ct, int(configured)))
            inner_enabled = outer_workers == 1
        elif resources_ct <= 2:
            outer_workers = max(1, min(resources_ct, max(1, cpu // 2)))
            inner_enabled = True
        else:
            outer_workers = max(1, min(resources_ct, cpu))
            inner_enabled = False

    inner_workers = 1
    if inner_enabled:
        inner_workers = 3
    return outer_workers, inner_enabled, inner_workers
