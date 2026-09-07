"""Empirical upper bounds for observation alongside existing scaling advice."""
from __future__ import annotations

import logging
import hashlib
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from resource_predict.pipeline.realized_error import DB_NAME, _basis, _json, _unit

logger = logging.getLogger(__name__)
TARGET_COVERAGE = 0.95
MIN_SAMPLES = 60
MAX_SAMPLES = 500
HORIZONS = ((0, 3600000), (3600000, 21600000), (21600000, 86400000), (86400000, 2**63 - 1))


def _charts(item: dict):
    for metric, chart in item.get("charts_forecast", {}).items():
        yield "", metric, chart, item.get("forecast_diagnostics", {}).get(metric, {})
    for container, metrics in item.get("container_charts_forecast", {}).items():
        for metric, chart in metrics.items():
            yield container, metric, chart, chart.get("forecast_diagnostics", {})


def _calibrate_curve(db, item, container, metric, chart, diagnostics, retention_days):
    provenance = diagnostics.get("provenance", {})
    origin = provenance.get("data_end_ms")
    generated = provenance.get("generated_at_epoch_ms")
    result = {"version": 1, "mode": "observe", "target_coverage": TARGET_COVERAGE,
              "status": "insufficient_samples", "upper": [], "buckets": [],
              "min_samples": MIN_SAMPLES, "max_samples": MAX_SAMPLES,
              "basis": _basis(item, container, metric), "unit": _unit(item, container, metric)}
    timestamps = chart.get("x_pred_ms", [])
    values = chart.get("preds_future", {}).get(chart.get("best_method"), [])
    if not isinstance(origin, int) or not isinstance(generated, int) or not all(
        provenance.get(key) for key in ("model_version", "config_hash")
    ):
        result["status"] = "missing_provenance"
        return result
    if not timestamps or len(timestamps) != len(values):
        raise ValueError("unaligned calibration curve")
    result["calibrated_at_epoch_ms"] = generated
    result["data_end_ms"] = origin
    samples = {}
    if db is not None:
        # A single ordered cursor replaces repeated window sorts; retain at most 500 per bucket.
        query = (
            "SELECT p.actual-p.predicted,p.target_ms,p.scored_at_ms,c.batch,"
            "p.target_ms-c.data_end_ms FROM curves c JOIN points p ON p.curve_id=c.id "
            "WHERE c.resource_id=? AND c.container=? AND c.metric=? AND c.model=? AND c.unit=? "
            "AND c.basis=? AND p.actual IS NOT NULL AND p.scored_at_ms<? AND p.target_ms<=? "
            "AND c.issued_ms>=? AND p.target_ms>c.data_end_ms "
            "AND json_extract(c.provenance,'$.model_version')=? "
            "AND json_extract(c.provenance,'$.config_hash')=? "
            "ORDER BY p.target_ms DESC,c.issued_ms DESC,c.id DESC"
        )
        seen = {bucket: set() for bucket in range(len(HORIZONS))}
        for residual, target, scored, batch, horizon in db.execute(query, (
            item["resource_id"], container, metric, chart["best_method"], _unit(item,container,metric),
            _basis(item,container,metric), generated,min(origin,generated),generated-retention_days*86400000,
            provenance["model_version"],provenance["config_hash"],
        )):
            bucket = next(i for i,(low,high) in enumerate(HORIZONS) if low < horizon <= high)
            if len(seen[bucket]) < MAX_SAMPLES and target not in seen[bucket]:
                samples.setdefault(bucket, []).append((residual,target,scored,batch))
                seen[bucket].add(target)
            if all(len(targets) == MAX_SAMPLES for targets in seen.values()):
                break
    cache = {}
    for target, prediction in zip(timestamps, values):
        if not isinstance(target, int) or target <= origin or not math.isfinite(float(prediction)):
            raise ValueError("invalid calibration point")
        bucket = next(i for i, (low, high) in enumerate(HORIZONS) if low < target-origin <= high)
        if bucket not in cache:
            rows = samples.get(bucket, [])
            residuals = [float(row[0]) for row in rows if math.isfinite(float(row[0]))]
            count = len(residuals)
            margin = None
            if count >= MIN_SAMPLES:
                rank = math.ceil((count + 1) * TARGET_COVERAGE)
                margin = max(0.0, sorted(residuals)[rank - 1])
            cache[bucket] = margin
            result["buckets"].append({"horizon_bucket": bucket, "sample_count": count, "margin": margin,
                                      "sample_digest": hashlib.sha256(_json(rows).encode()).hexdigest(),
                                      "latest_target_ms": rows[0][1] if rows else None,
                                      "oldest_target_ms": rows[-1][1] if rows else None})
        margin = cache[bucket]
        result["upper"].append(float(prediction) + margin if margin is not None else None)
    known = sum(value is not None for value in result["upper"])
    result["status"] = "calibrated" if known == len(values) else "partial" if known else "insufficient_samples"
    return result


def refresh_calibration_advice(items: list[dict]) -> None:
    """Rebuild annotations after partial merges; existing action/targets are untouched."""
    for item in items:
        advice = item.get("scaling_advice")
        if not isinstance(advice, dict):
            continue
        rows = []
        for container, metric, chart, _ in _charts(item):
            block = chart.get("calibration")
            if block:
                comparable = block.get("basis") == _basis(item, container, metric)
                upper = [x for x in block.get("upper", []) if x is not None]
                rows.append({"container": container or None, "metric": metric,
                             "status": block["status"] if comparable or not upper else "basis_changed",
                             "upper_peak": max(upper) if upper and comparable else None,
                             "complete": block["status"] == "calibrated" and comparable,
                             "unit": block.get("unit", _unit(item, container, metric))})
        active = advice.get("calibration_activation",{}).get("status")=="active"
        advice["prediction_upper_bound"] = {"mode": "active" if active else "observe", "target_coverage": TARGET_COVERAGE,
                                             "metrics": rows, "applied_to_targets": active}


def calibrate_forecasts(base: Path, items: list[dict], *, retention_days: int = 7) -> None:
    """Read one consistent ledger snapshot without creating or changing it."""
    path = Path(base) / DB_NAME
    db = None
    try:
        if path.exists():
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            db.execute("BEGIN")
        for item in items:
            for container, metric, chart, diagnostics in _charts(item):
                try:
                    block = _calibrate_curve(db, item, container, metric, chart, diagnostics, retention_days)
                    _json(block)  # Never publish nonfinite upper bounds.
                    chart["calibration"] = block
                except (ValueError, TypeError, sqlite3.Error, OverflowError) as exc:
                    logger.warning("[calibration] %s/%s/%s: %s", item.get("resource_id"), container, metric, exc)
                    chart["calibration"] = {"version": 1, "mode": "observe", "status": "failed", "upper": []}
    except sqlite3.Error as exc:
        logger.warning("[calibration] ledger unavailable: %s", exc)
        for item in items:
            for _, _, chart, _ in _charts(item):
                chart["calibration"] = {"version": 1, "mode": "observe", "status": "failed", "upper": []}
    finally:
        if db is not None:
            with closing(db):
                db.rollback()
    refresh_calibration_advice(items)
