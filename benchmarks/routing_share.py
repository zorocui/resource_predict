"""Offline preflight and allowlisted aggregate export; never uploads data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.routing_pilot import METHODS, snapshot_series

FEATURES = ("points", "mean", "std", "range", "slope", "step_seconds", "daily_corr", "weekly_corr")
REASONS = ("invalid_datetime_index", "unordered_or_duplicate_time", "short_history", "missing_or_irregular")


def distribution(values):
    values = np.asarray(list(values), dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"count": 0, "mean": None, "p50": None, "p95": None}
    return {"count": len(values), "mean": float(np.mean(values)),
            "p50": float(np.quantile(values, .5)), "p95": float(np.quantile(values, .95))}


def resource_group(row):
    # Internal grouping only. Neither this value nor its hash is exported.
    try:
        identity = json.loads(row["series_id"])
    except (ValueError, TypeError):
        return row["series_id"]
    return identity[0] if isinstance(identity, list) and len(identity) == 3 else row["series_id"]


def probe(rows, mode):
    complete = [r for r in rows if r["status"] == "paired" and all(
        phase["status"] == "ok" for phases in r["methods"].values() for phase in phases.values())]
    result = {"status": "insufficient_data", "train_rows": 0, "test_rows": 0,
              "target": "delta_rmse / max(abs(history_mean), 0.01)",
              "method": "standardized_ridge_alpha_1", "selection_fraction": 0.5}
    if mode == "resource":
        groups = sorted({resource_group(r) for r in complete})
        result["resource_groups"] = len(groups)
        if len(groups) < 10:
            return result
        rng = np.random.default_rng(42)
        rng.shuffle(groups)
        held = set(groups[int(.7 * len(groups)):])
        train = [r for r in complete if resource_group(r) not in held]
        test = [r for r in complete if resource_group(r) in held]
    else:
        origins = sorted({r["origin_time"] for r in complete})
        if len(origins) < 5:
            return result
        cutoff = origins[int(.7 * len(origins))]
        test = [r for r in complete if r["origin_time"] >= cutoff]
        # Labels must have arrived before the earliest held-out routing feature cutoff.
        known_by = min(pd.Timestamp(r["feature_end"]) for r in test)
        train = [r for r in complete if pd.Timestamp(r["test_end"]) < known_by]
    result.update(train_rows=len(train), test_rows=len(test))
    if len(train) < 30 or len(test) < 10:
        return result

    def arrays(records):
        x = np.array([[r["features"].get(f, 0.0) for f in FEATURES] for r in records])
        y = np.array([r["delta_rmse"] / max(abs(r["features"]["mean"]), .01) for r in records])
        return x, y

    x_train, y_train = arrays(train)
    x_test, y_test = arrays(test)
    mean, scale = x_train.mean(axis=0), x_train.std(axis=0)
    scale[scale < 1e-12] = 1
    x_train, x_test = (x_train - mean) / scale, (x_test - mean) / scale
    weights = np.linalg.solve(x_train.T @ x_train + np.eye(len(FEATURES)),
                              x_train.T @ (y_train - y_train.mean()))
    predictions = x_test @ weights + y_train.mean()
    count = max(1, len(test) // 2)
    chosen = np.argsort(-predictions, kind="stable")[:count]
    result.update(status="ok", ridge_mae=float(np.mean(np.abs(predictions-y_test))),
                  constant_mae=float(np.mean(np.abs(y_train.mean()-y_test))),
                  top_half_mean_normalized_gain=float(np.mean(y_test[chosen])),
                  random_half_expected_mean_normalized_gain=float(np.mean(y_test)),
                  selected_extra_wall_seconds=float(sum(test[i]["delta_wall_seconds"] for i in chosen)))
    return result


def export_summary(report, rows):
    """Construct new output from numeric aggregates, not redaction of the private report."""
    metadata = report["metadata"]
    paired = [r for r in rows if r["status"] == "paired"]
    complete = [r for r in paired if all(p["status"] == "ok" for m in r["methods"].values() for p in m.values())]
    return {
        "schema": "routing-share-v1",
        "source": metadata.get("source") if metadata.get("source") in ("synthetic", "unverified_local_snapshot") else "unknown",
        "protocol": metadata.get("protocol") if metadata.get("protocol") in ("routing-pilot-v1", "routing-pilot-v2") else "unknown",
        "snapshot_unchanged": metadata.get("index_unchanged") is True if "index_unchanged" in metadata else None,
        "counts": {"rows": len(rows), "series": len({r["series_id"] for r in rows}),
                   "resources": len({resource_group(r) for r in rows}), "paired": len(paired),
                   "all_candidates_successful_pairs": len(complete),
                   "failed": sum(r["status"] == "failed" for r in rows),
                   "skipped": sum(r["status"] == "skipped" for r in rows),
                   "positive_gain": sum(r["delta_rmse"] > 1e-12 for r in paired),
                   "negative_gain": sum(r["delta_rmse"] < -1e-12 for r in paired),
                   "unchanged": sum(abs(r["delta_rmse"]) <= 1e-12 for r in paired)},
        "skip_reasons": {reason: sum(r.get("reason") == reason for r in rows) for reason in REASONS},
        "method_failures": {m: sum(p["status"] == "failed" for r in rows
                                   for p in r.get("methods", {}).get(m, {}).values()) for m in METHODS},
        "training_points": distribution(r["features"]["points"] for r in paired),
        "history_days": distribution(r["features"]["points"] * r["features"]["step_seconds"] / 86400 for r in paired),
        "horizon_hours": distribution((pd.Timestamp(r["test_end"])-pd.Timestamp(r["origin_time"])).total_seconds()/3600
                                      + r["features"]["step_seconds"]/3600 for r in paired),
        "normalized_gain": distribution(r["delta_rmse"]/max(abs(r["features"]["mean"]), .01) for r in paired),
        "extra_wall_seconds": distribution(r["delta_wall_seconds"] for r in paired),
        "resource_holdout_probe": probe(rows, "resource"),
        "time_holdout_probe": probe(rows, "time"),
        "limits": "Descriptive aggregates; no identifiers, hashes, timestamps, paths, raw errors or curves. No privacy guarantee. Resource holdout is not business holdout. Top-half comparison is equal count, not equal compute budget. No production-benefit claim.",
    }


def preflight(raw_dir, limit, seed, horizon, origins):
    counts = {"series": 0, "eligible": 0, "short": 0, "irregular_or_missing": 0}
    spans, steps = [], []
    for _, series in snapshot_series(raw_dir, limit, seed):
        counts["series"] += 1
        if len(series) < (origins + 1)*horizon + 48:
            counts["short"] += 1
            continue
        gaps = np.diff(series.index.asi8)
        if (series.index.hasnans or not series.index.is_unique or not series.index.is_monotonic_increasing
                or not np.isfinite(series.to_numpy(dtype=float)).all() or not np.all(gaps == gaps[0])):
            counts["irregular_or_missing"] += 1
            continue
        counts["eligible"] += 1
        steps.append(float(gaps[0]/1e9))
        spans.append(float((series.index[-1]-series.index[0]).total_seconds()/86400))
    return {"schema": "routing-preflight-v1", "counts": counts, "history_days": distribution(spans),
            "horizon_hours": distribution(step*horizon/3600 for step in steps),
            "model_fits_planned": counts["eligible"]*origins*len(METHODS)*2}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--run-dir", type=Path)
    action.add_argument("--check-raw", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--origins", type=int, default=3)
    args = parser.parse_args()
    if min(args.limit, args.horizon, args.origins) < 1:
        parser.error("limit, horizon and origins must be positive")
    if args.output.exists():
        parser.error("output exists; choose a new filename")
    if args.check_raw:
        summary = preflight(args.check_raw, args.limit, args.seed, args.horizon, args.origins)
    else:
        report = json.loads((args.run_dir / "report.json").read_text(encoding="utf-8"))
        if report["metadata"].get("index_unchanged") is False:
            parser.error("snapshot changed during replay; rerun on a frozen copy")
        rows = [json.loads(line) for line in (args.run_dir / "pairs.jsonl").read_text(encoding="utf-8").splitlines()]
        summary = export_summary(report, rows)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print("Aggregate file written. Review internally before sharing.")


if __name__ == "__main__":
    main()
