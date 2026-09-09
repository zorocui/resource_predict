"""Compare real metric fits, including pool startup/IPC, on deterministic inputs.

Run from the repository root: python -m benchmarks.multicore_forecast --help
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import platform
import time

import numpy as np
import pandas as pd

from resource_predict.pipeline._types import WorkerContext
from resource_predict.pipeline.parallel import execute_metric_jobs
from resource_predict.pipeline.plan import resolve_execution_plan
from resource_predict.settings import settings

MODELS = {"arima", "sarima", "prophet", "seasonal_naive", "rolling_mean"}
BACKENDS = {"serial", "thread", "process"}


def _outputs_match(left, right):
    if isinstance(left, dict):
        return (isinstance(right, dict) and left.keys() == right.keys()
                and all(_outputs_match(value, right[key]) for key, value in left.items()))
    if isinstance(left, (list, np.ndarray)):
        return (np.shape(left) == np.shape(right)
                and bool(np.allclose(left, right, rtol=1e-7, atol=1e-9, equal_nan=True)))
    if isinstance(left, (int, float, np.number)):
        return bool(np.isclose(left, right, rtol=1e-7, atol=1e-9, equal_nan=True))
    return left == right


def _canonical(result):
    predictions, scores, best, future, _, diagnostics = result
    return {"test": {method: values.to_numpy() for method, values in predictions.items()},
            "future": {method: values.to_numpy() for method, values in future.items()},
            "scores": scores, "best_method": best,
            "config_hash": diagnostics["provenance"]["config_hash"]}


def run_benchmark(*, jobs=16, points=360, workers=4, models=None, backends=None):
    models = list(models or ["arima"])
    backends = list(backends or ["serial", "thread", "process"])
    if jobs < 1 or points < 32 or workers < 0:
        raise ValueError("jobs must be positive, points at least 32 and workers non-negative")
    if not set(models) <= MODELS or not set(backends) <= BACKENDS:
        raise ValueError("unknown model or backend")
    snapshot = settings.freeze()
    ctx = WorkerContext(test_size=min(24, max(4, points // 5)), future_steps=6,
                        active_methods=models, forecast_config={"prophet_routing_enabled": False},
                        metric_filter_by_id={}, metric_partial_enabled=False, existing_partial_ids=set(),
                        sample_interval_seconds=3600)
    index = pd.date_range("2026-01-01", periods=points, freq="h")
    rng = np.random.default_rng(20260909)
    # Prepare identical inputs outside the timed region for every backend.
    inputs = [(i, "", "cpu", pd.Series(0.4 + 0.1 * np.sin(np.arange(points) * 2 * np.pi / 24 + i / 7)
                                      + rng.normal(0, 0.01, points), index=index)) for i in range(jobs)]
    reports, canonical = {}, {}
    for requested in dict.fromkeys(backends):
        plan = resolve_execution_plan(jobs, models, backend=requested, max_workers=workers)
        stats, results, failures = {}, {}, Counter()
        started = time.perf_counter()
        for slot, container, metric, result in execute_metric_jobs(iter(inputs), ctx, snapshot, plan, stats):
            results[(slot, container, metric)] = _canonical(result)
            failures.update(result[5].get("method_failures", {}).keys())
        wall = time.perf_counter() - started
        canonical[requested] = results
        reports[requested] = {**stats, "requested_backend": requested, "wall_seconds": wall,
                              "metric_tasks_per_second": jobs / wall,
                              "model_failure_task_counts": dict(failures)}
    reference = canonical.get("serial")
    for requested, report in reports.items():
        report["speedup_vs_serial"] = (reports["serial"]["wall_seconds"] / report["wall_seconds"]
                                        if reference is not None else None)
        report["outputs_match"] = (_outputs_match(reference, canonical[requested])
                                   if reference is not None else None)
    return {"scope": "model_fit_only", "jobs": jobs, "points": points, "models": models,
            "platform": platform.platform(), "python": platform.python_version(), "runs": reports,
            "comparison_tolerance": {"rtol": 1e-7, "atol": 1e-9},
            "limits": "Deterministic synthetic series with real existing model fits. Wall time includes "
                      "pool startup, IPC, result comparison preparation and shutdown; excludes collection, "
                      "archiving, decision assembly and report writes. Backends run sequentially in the "
                      "requested order; later runs may benefit from warm libraries/caches. Results describe "
                      "this machine and workload, not production throughput at 10,000 resources. "
                      "Null comparison/speedup means serial was not requested. Model failures are reported "
                      "because existing baseline fallback must not be mistaken for successful requested fits."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=16, help="number of metric tasks")
    parser.add_argument("--points", type=int, default=360)
    parser.add_argument("--workers", type=int, default=4, help="0 means automatic")
    parser.add_argument("--models", default="arima", help="comma-separated: " + ",".join(sorted(MODELS)))
    parser.add_argument("--backends", default="serial,thread,process", help="comma-separated execution order")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(jobs=args.jobs, points=args.points, workers=args.workers,
                               models=[name.strip() for name in args.models.split(",")],
                               backends=[name.strip() for name in args.backends.split(",")])
    except ValueError as exc:
        parser.error(str(exc))
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 1 if any(row["outputs_match"] is False for row in report["runs"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
