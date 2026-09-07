"""Reproducible disk-backed feedback benchmark; no production data or network.

Run as python -m benchmarks.forecast_feedback_benchmark --resources 10000.
Database lives in a temporary directory unless --database is supplied.
This measures the feedback ledger, not model fitting or Prometheus ingestion.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from resource_predict.pipeline.realized_error import _SCHEMA, _report, _score_evidence, _basis, _unit, _json
from resource_predict.pipeline.shadow_evaluation import SCHEMA as SHADOW_SCHEMA
from resource_predict.pipeline.calibration import _calibrate_curve

NOW = 1_788_700_000_000
METRICS = ("cpu_request", "cpu_limit", "memory_request", "memory_limit")


def process_usage():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("peak_rss", "rss", "peak_paged", "paged",
                                                     "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        handle = kernel.GetCurrentProcess()
        memory, counters = Memory(), IO()
        memory.cb = ctypes.sizeof(memory)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
        kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO)]
        if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(memory),memory.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GetProcessIoCounters(handle,ctypes.byref(counters)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"peak_rss_bytes": memory.peak_rss, "process_read_bytes": counters.read_bytes,
                "process_write_bytes": counters.write_bytes}
    import resource as usage
    stats = usage.getrusage(usage.RUSAGE_SELF)
    return {"peak_rss_bytes": stats.ru_maxrss*(1 if sys.platform == "darwin" else 1024),
            "block_write_bytes": stats.ru_oublock*512}


def resource(index: int, containers: int) -> dict:
    return {"resource_id": f"k8s:benchmark:{index}", "resource_type": "k8s_workload",
            "spec": {"replicas_observed": 2, "containers": {
                f"app-{c}": {"cpu_request_cores": 0.5, "cpu_limit_cores": 1,
                             "memory_request_gb": 0.5, "memory_limit_gb": 1} for c in range(containers)}},
            "container_metric_modes": {f"app-{c}": {m: "usage/"+m for m in METRICS} for c in range(containers)}}


def seed(db, resources, containers, batches, points):
    db.executescript(_SCHEMA + SHADOW_SCHEMA)
    if db.execute("SELECT COUNT(*) FROM curves").fetchone()[0]:
        return
    provenance = _json({"model_version": "benchmark-v1", "config_hash": "benchmark"})
    with db:
        for b in range(batches):
            issued = NOW - (batches-b)*86400000
            batch = f"benchmark-{b}"
            db.execute("INSERT INTO batches VALUES (?,?)", (batch, issued))
            for r in range(resources):
                item = resource(r, containers)
                for c in range(containers):
                    container = f"app-{c}"
                    for metric in METRICS:
                        curve = db.execute(
                            "INSERT INTO curves(batch,resource_id,container,metric,model,unit,data_end_ms,issued_ms,basis,provenance,eligible) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                            (batch, item["resource_id"], container, metric, "seasonal_naive", _unit(item,container,metric),
                             issued, issued+1, _basis(item,container,metric), provenance),
                        ).lastrowid
                        db.executemany(
                            "INSERT INTO points(curve_id,target_ms,predicted,actual,scored_at_ms) VALUES (?,?,?,?,?)",
                            ((curve, issued+(p+1)*600000, 0.2, 0.3 if b<batches-1 else None,
                              issued+(p+1)*600000+1 if b<batches-1 else None) for p in range(points)),
                        )
    db.execute("ANALYZE")


def observations(resources, containers, points):
    for r in range(resources):
        item = resource(r, containers)
        item["observation_evidence"] = {
            "schema_version": 1, "source": "benchmark", "resource_type": item["resource_type"],
            "spec": item["spec"], "container_metric_modes": item["container_metric_modes"],
            "container_metrics": {f"app-{c}": {m: {
                "timestamps": [NOW-86400000+(p+1)*600000 for p in range(points)],
                "values": [0.3]*points,
            } for m in METRICS} for c in range(containers)},
        }
        yield item


def measure(path, resources, containers, batches, points):
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as check:
            names = {row[0] for row in check.execute("SELECT name FROM batches")}
            expected = {f"benchmark-{b}" for b in range(batches)}
            count = check.execute("SELECT COUNT(*) FROM curves").fetchone()[0]
            foreign = check.execute("SELECT 1 FROM curves WHERE resource_id NOT LIKE 'k8s:benchmark:%' LIMIT 1").fetchone()
            point_count = check.execute("SELECT COUNT(*) FROM points").fetchone()[0]
            if names != expected or count != resources*containers*batches*4 or foreign or point_count != count*points:
                raise ValueError("existing database is not the requested synthetic benchmark; refusing to modify it")
    results = {"resources": resources, "containers_per_resource": containers, "metrics_per_container": 4,
               "daily_batches": batches, "points_per_curve": points}
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA temp_store=FILE")
        started = time.perf_counter()
        seed(db, resources, containers, batches, points)
        results["seed_seconds"] = time.perf_counter()-started
        print(json.dumps({"stage": "seed", **results}), flush=True)
        # Reset only benchmark's last day for a repeatable incremental scoring measurement.
        with db:
            db.execute("UPDATE points SET actual=NULL,scored_at_ms=NULL WHERE curve_id IN "
                       "(SELECT id FROM curves WHERE batch=?)", (f"benchmark-{batches-1}",))
        started = time.perf_counter()
        with db:
            results["scoring"] = _score_evidence(db, observations(resources,containers,points), NOW)
        results["score_seconds"] = time.perf_counter()-started
        print(json.dumps({"stage": "score", "seconds": results["score_seconds"]}), flush=True)
        started = time.perf_counter()
        report = _report(db, NOW, 7)
        results["report_seconds"] = time.perf_counter()-started
        results["coverage"] = report["coverage"]
        results["report_digest"] = hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
        print(json.dumps({"stage": "report", "seconds": results["report_seconds"]}), flush=True)
        started = time.perf_counter()
        statuses = {}
        digest = hashlib.sha256()
        for r in range(resources):
            item = resource(r,containers)
            for c in range(containers):
                for metric in METRICS:
                    chart = {"best_method": "seasonal_naive", "x_pred_ms": [NOW+(p+1)*600000 for p in range(points)],
                             "preds_future": {"seasonal_naive": [0.2]*points}}
                    diagnostics = {"provenance": {"data_end_ms": NOW, "generated_at_epoch_ms": NOW,
                                                   "model_version": "benchmark-v1", "config_hash": "benchmark"}}
                    result = _calibrate_curve(db,item,f"app-{c}",metric,chart,diagnostics,7)
                    digest.update(json.dumps(result,sort_keys=True).encode())
                    statuses[result["status"]] = statuses.get(result["status"],0)+1
        results["calibrate_seconds"] = time.perf_counter()-started
        results["calibration_statuses"] = statuses
        results["calibration_digest"] = digest.hexdigest()
        results["database_bytes"] = path.stat().st_size
        results["points"] = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        results["process_usage"] = process_usage()
    return results


def main():
    global _report, _calibrate_curve
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=int, default=10000)
    parser.add_argument("--containers", type=int, default=1)
    parser.add_argument("--batches", type=int, default=7)
    parser.add_argument("--points", type=int, default=24)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--reference-dir", type=Path, help="Optional saved pre-optimization modules for comparison")
    args = parser.parse_args()
    if min(args.resources,args.containers,args.batches,args.points)<=0:
        parser.error("load dimensions must be positive")
    if args.reference_dir:
        loaded = []
        for name in ("realized", "calibration"):
            spec = importlib.util.spec_from_file_location(name, args.reference_dir/f"feedback_baseline_{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loaded.append(module)
        _report, _calibrate_curve = loaded[0]._report, loaded[1]._calibrate_curve
    with tempfile.TemporaryDirectory(prefix="forecast-feedback-") as temporary:
        path = args.database or Path(temporary)/"benchmark.sqlite3"
        result = measure(path,args.resources,args.containers,args.batches,args.points)
        print(json.dumps(result,indent=2), flush=True)
        if args.result:
            args.result.parent.mkdir(parents=True,exist_ok=True)
            args.result.write_text(json.dumps(result,indent=2),encoding="utf-8")


if __name__ == "__main__":
    main()
