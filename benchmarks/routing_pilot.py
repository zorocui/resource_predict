"""Stage-one paired replay. Run with python -m benchmarks.routing_pilot."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
import time

import numpy as np
import pandas as pd

from resource_predict.data.raw_store import RawResourceStore
from resource_predict.pipeline.forecasting import forecast_by_method
from resource_predict.settings import settings
from resource_predict.pipeline.anomaly import anomaly_profile
from resource_predict.pipeline.prophet_routing import prophet_routing_decision

FAST = ("seasonal_naive", "rolling_mean")
METHODS = FAST + ("prophet",)


def synthetic(seed):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-01-01", periods=24 * 28, freq="h")
    t = np.arange(len(index))
    patterns = {
        "stable": np.full(len(t), .3),
        "daily": .4 + .2 * np.sin(2 * np.pi * t / 24),
        "trend": .15 + .0008 * t,
        "weekly": .4 + .2 * np.sin(2 * np.pi * t / 168),
        "shift": np.where(t < 24 * 20, .25, .65),
        "bursts": .25 + .5 * (rng.random(len(t)) < .08),
    }
    for name, values in patterns.items():
        yield name, pd.Series(np.clip(values + rng.normal(0, .015, len(t)), 0, 1), index=index)


def snapshot_series(path, limit, seed):
    store = RawResourceStore(path)
    ids = sorted(store.resource_ids())
    rng = np.random.default_rng(seed)
    rng.shuffle(ids)
    for rid in ids[:limit]:
        item = store.get(rid)
        containers = item.get("container_metrics", {})
        groups = containers.items() if containers else [("", item)]
        for container, metrics in sorted(groups):
            for metric, series in sorted(metrics.items()):
                if isinstance(series, pd.Series):
                    yield json.dumps([rid, container, metric]), series


def features(history):
    values = history.to_numpy(dtype=float)
    std = float(np.std(values))
    step = float((history.index[1] - history.index[0]).total_seconds())
    correlations = {}
    for name, seconds in (("daily_corr", 86400), ("weekly_corr", 604800)):
        lag = max(1, round(seconds / step))
        correlations[name] = (float(np.corrcoef(values[:-lag], values[lag:])[0, 1])
                              if len(values) > lag * 2 and np.std(values[:-lag]) > 1e-12
                              and np.std(values[lag:]) > 1e-12 else 0.0)
    return {**correlations, "points": len(values), "mean": float(np.mean(values)), "std": std,
            "range": float(np.ptp(values)),
            "slope": float(np.polyfit(np.arange(len(values)), values, 1)[0]),
            "step_seconds": step}


def measure(method, history, actual, predict):
    started = time.perf_counter()
    result = {"status": "failed"}
    try:
        values = np.asarray(predict(method, history, len(actual)).yhat, dtype=float)
        if values.shape != actual.shape or not np.isfinite(values).all():
            raise ValueError("invalid prediction")
        errors = values - actual.to_numpy(dtype=float)
        result.update(status="ok", rmse=float(np.sqrt(np.mean(errors ** 2))),
                      mae=float(np.mean(np.abs(errors))),
                      underprediction=float(np.mean(np.maximum(-errors, 0))))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["wall_seconds"] = time.perf_counter() - started
    return result


def replay(series, horizon, origins, predict=forecast_by_method):
    """Features precede validation; test never selects a model. No interpolation."""
    if not isinstance(series.index, pd.DatetimeIndex) or series.index.hasnans:
        return [{"status": "skipped", "reason": "invalid_datetime_index"}]
    if not series.index.is_monotonic_increasing or not series.index.is_unique:
        return [{"status": "skipped", "reason": "unordered_or_duplicate_time"}]
    if len(series) < (origins + 1) * horizon + 48:
        return [{"status": "skipped", "reason": "short_history"}]
    rows = []
    for origin in range(len(series) - origins * horizon, len(series), horizon):
        observed = series.iloc[:origin + horizon]
        gaps = np.diff(observed.index.asi8)
        if not np.isfinite(observed.to_numpy(dtype=float)).all() or not np.all(gaps == gaps[0]):
            rows.append({"status": "skipped", "reason": "missing_or_irregular", "origin": origin})
            continue
        train, valid = series.iloc[:origin-horizon], series.iloc[origin-horizon:origin]
        test = series.iloc[origin:origin+horizon]
        row = {"origin": origin, "origin_time": str(test.index[0]),
               "feature_end": str(train.index[-1]), "validation_end": str(valid.index[-1]),
               "test_end": str(test.index[-1]), "features": features(train), "methods": {}}
        anomaly = anomaly_profile(train, zscore_threshold=float(settings.forecast.anomaly_route_zscore_threshold))
        rule = prophet_routing_decision(train, active_methods=METHODS, anomaly=anomaly,
                                       enabled=True, mode="auto")
        row["auto_rule_run"] = rule["decision"] == "run"
        # Rotate order to reduce systematic warm-start bias; do not execute in parallel.
        offset = len(rows) % len(METHODS)
        order = METHODS[offset:] + METHODS[:offset]
        for method in order:
            row["methods"][method] = {
                "validation": measure(method, train, valid, predict),
                "test": measure(method, series.iloc[:origin], test, predict)}
        for label, candidates in (("baseline", FAST), ("enhanced", METHODS)):
            available = [m for m in candidates if row["methods"][m]["validation"]["status"] == "ok"]
            if not available:
                row[label] = {"status": "failed", "reason": "no_validation_candidate"}
                continue
            selected = min(available, key=lambda m: row["methods"][m]["validation"]["rmse"])
            score = row["methods"][selected]["test"]
            row[label] = {**score, "selected": selected,
                          "estimated_workflow_wall_seconds": sum(
                              row["methods"][m]["validation"]["wall_seconds"] for m in candidates)
                          + score["wall_seconds"]}
        row["status"] = "paired" if all(row[x]["status"] == "ok" for x in ("baseline", "enhanced")) else "failed"
        if row["status"] == "paired":
            row["delta_rmse"] = row["baseline"]["rmse"] - row["enhanced"]["rmse"]
            row["delta_wall_seconds"] = (row["enhanced"]["estimated_workflow_wall_seconds"]
                                           - row["baseline"]["estimated_workflow_wall_seconds"])
        rows.append(row)
    return rows


def summarize(rows):
    paired = [r for r in rows if r["status"] == "paired"]
    groups = {}
    for row in paired:
        groups.setdefault(row["series_id"], []).append(row["delta_rmse"])
    return {"attempts": len(rows), "paired": len(paired),
            "method_phase_failures": {m: sum(
                phase["status"] == "failed" for r in rows
                for phase in r.get("methods", {}).get(m, {}).values()) for m in METHODS},
            "failed": sum(r["status"] == "failed" for r in rows),
            "skipped": sum(r["status"] == "skipped" for r in rows),
            "series_mean_delta_rmse": {key: float(np.mean(values)) for key, values in groups.items()},
            "prophet_selected": sum(r["enhanced"]["selected"] == "prophet" for r in paired),
            "positive_gain_pairs": sum(r["delta_rmse"] > 1e-12 for r in paired),
            "negative_gain_pairs": sum(r["delta_rmse"] < -1e-12 for r in paired),
            "mean_delta_wall_seconds": float(np.mean([r["delta_wall_seconds"] for r in paired])) if paired else None,
            "conclusion": "Pilot only: no claim of production benefit or learnability; independent resources and later-time validation required."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--limit", type=int, default=12, help="Maximum sampled resources in raw mode")
    parser.add_argument("--horizon", type=int, default=24, help="Forecast and validation steps")
    parser.add_argument("--origins", type=int, default=3)
    args = parser.parse_args()
    if min(args.limit, args.horizon, args.origins) < 1:
        parser.error("limit, horizon and origins must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    metadata = {"protocol": "routing-pilot-v2", "source": "unverified_local_snapshot" if args.raw_dir else "synthetic",
                "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                "platform": platform.platform(), "python": platform.python_version(),
                "versions": {p: importlib.metadata.version(p) for p in ("numpy", "pandas", "prophet", "statsmodels")},
                "forecast_settings": asdict(settings.forecast),
                "timing": "Serial wall time; workflow estimates reuse measured phases. Includes backend waiting, not CPU accounting. No hard timeout."}
    if args.raw_dir:
        index_path = args.raw_dir / "raw_index.json"
        metadata["index_sha256"] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    source = snapshot_series(args.raw_dir, args.limit, args.seed) if args.raw_dir else synthetic(args.seed)
    rows = []
    with (args.output / "pairs.jsonl").open("w", encoding="utf-8") as stream:
        for name, series in source:
            digest = hashlib.sha256(pd.util.hash_pandas_object(series).values.tobytes()).hexdigest()
            for row in replay(series, args.horizon, args.origins):
                row.update(series_id=name, series_sha256=digest)
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                rows.append(row)
            print(f"completed {name}", flush=True)
    if args.raw_dir:
        metadata["index_unchanged"] = metadata["index_sha256"] == hashlib.sha256(index_path.read_bytes()).hexdigest()
    metadata["experiment_wall_seconds"] = time.perf_counter() - started
    report = {"metadata": metadata, "summary": summarize(rows)}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
