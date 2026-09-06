"""Immutable, streamed archives of freshly generated selected future forecasts."""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)
_ARCHIVE_NAME = re.compile(r"forecast_(\d{13})_[0-9a-f]{32}\.jsonl\.gz")


def _selected_forecast(chart: dict, diagnostics: dict) -> dict:
    selected = chart["best_method"]
    timestamps = chart["x_pred_ms"]
    values = chart["preds_future"][selected]
    if not timestamps or len(timestamps) != len(values):
        raise ValueError("selected forecast timestamps and values must be nonempty and aligned")
    return {
        "x_pred_ms": timestamps,
        "yhat": values,
        "selected_model": selected,
        "provenance": diagnostics.get("provenance", {}),
    }


def _archive_record(item: dict, run_id: str) -> dict:
    record = {"schema_version": 1, "run_id": run_id}
    for field in (
        "resource_id", "resource_type", "spec", "data_quality",
        "container_data_quality", "container_metric_modes", "scaling_advice",
    ):
        if field in item:
            record[field] = item[field]
    diagnostics = item.get("forecast_diagnostics", {})
    record["forecasts"] = {
        metric: _selected_forecast(chart, diagnostics.get(metric, {}))
        for metric, chart in item.get("charts_forecast", {}).items()
    }
    record["container_forecasts"] = {
        container: {
            metric: _selected_forecast(chart, chart.get("forecast_diagnostics", {}))
            for metric, chart in metrics.items()
        }
        for container, metrics in item.get("container_charts_forecast", {}).items()
    }
    return record


def archive_forecasts(
    out_base: Path,
    resources_items: Iterable[dict],
    *,
    enabled: bool = True,
    retention_days: int = 7,
) -> dict[str, Any]:
    """Publish one gzip JSONL per batch; caller must pass fresh, unmerged items.

    Each line contains one resource and its selected metric/container forecasts.
    Failures raise to the caller; no completed archive is published on write failure.
    Retention applies only to our completed filenames in this output scope.
    """
    if not enabled:
        return {"status": "disabled", "path": None, "count": 0}
    if retention_days <= 0:
        raise ValueError("archive retention_days must be positive")
    now_ms = int(time.time() * 1000)
    run_id = f"{now_ms:013d}_{uuid.uuid4().hex}"
    directory = Path(out_base) / "forecast_history"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"forecast_{run_id}.jsonl.gz"
    temporary = directory / f".{destination.name}.tmp"
    count = 0
    created = False
    try:
        # Exclusive creation also protects a same-ID concurrent writer's temporary file.
        with temporary.open("xb") as raw:
            created = True
            with gzip.open(raw, "wt", encoding="utf-8") as stream:
                for item in resources_items:
                    record = _archive_record(item, run_id)
                    if not record["forecasts"] and not record["container_forecasts"]:
                        continue
                    json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    stream.write("\n")
                    count += 1
        if count == 0:
            return {"status": "empty", "path": None, "count": 0}
        if destination.exists():
            raise FileExistsError(destination)
        temporary.rename(destination)
    finally:
        if created:
            temporary.unlink(missing_ok=True)

    metadata = {"status": "completed", "path": str(destination), "count": count, "run_id": run_id}
    cutoff_ms = now_ms - retention_days * 86400 * 1000
    try:
        for path in sorted(directory.iterdir()):
            match = _ARCHIVE_NAME.fullmatch(path.name)
            if match and int(match.group(1)) < cutoff_ms and path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
    except OSError as exc:
        metadata["status"] = "completed_retention_failed"
        metadata["retention_error"] = str(exc)
        logger.warning("[forecast_archive] archive saved but retention failed: %s", exc)
    return metadata
