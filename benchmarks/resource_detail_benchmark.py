"""Synthetic benchmark for resource-level detail reads.

This benchmark intentionally bypasses model fitting so it measures the storage and
detail-serving path rather than forecasting time.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from resource_predict.data.io import atomic_write_json
from resource_predict.data.raw_store import write_raw_resource_dataset
from resource_predict.services.store.forecast_store import _SingleForecastStore
from resource_predict.settings import AppConfig, GenerationConfig


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark single-resource detail loading.")
    parser.add_argument("--resources", type=int, default=500)
    parser.add_argument("--points", type=int, default=2016)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--metadata-p95-target-ms", type=float, default=200.0)
    parser.add_argument("--chart-p95-target-ms", type=float, default=500.0)
    return parser.parse_args()


def _resource(resource_id: str, series: pd.Series) -> Dict[str, Any]:
    return {
        "resource_id": resource_id,
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
        "cpu": series,
        "memory": series,
        "disk": series,
    }


def _forecast_item(resource_id: str) -> Dict[str, Any]:
    block = {
        "preds": {"rolling_mean": [0.5] * 24},
        "x_pred_ms": list(range(24)),
        "preds_future": {"rolling_mean": [0.5] * 24},
        "metrics": {"rolling_mean": {"rmse": 0.01}},
        "best_method": "rolling_mean",
    }
    return {
        "resource_id": resource_id,
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
        "scaling_advice": {"action": "hold"},
        "charts_forecast": {"cpu": block, "memory": block, "disk": block},
    }


def _write_forecast_artifacts(base: Path, resource_ids: List[str], chunk_size: int = 25) -> None:
    details_dir = base / "details"
    details_dir.mkdir(parents=True, exist_ok=True)
    summary: List[Dict[str, Any]] = []
    for chunk_id, start in enumerate(range(0, len(resource_ids), chunk_size)):
        chunk_ids = resource_ids[start : start + chunk_size]
        file_name = f"part-{chunk_id:05d}.json"
        atomic_write_json(
            details_dir / file_name,
            {"resources": [_forecast_item(resource_id) for resource_id in chunk_ids]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for offset, resource_id in enumerate(chunk_ids):
            summary.append(
                {
                    "resource_id": resource_id,
                    "resource_type": "openstack_vm",
                    "spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
                    "scaling_advice": {"action": "hold"},
                    "detail_ref": {"file": file_name, "offset": offset},
                }
            )
    atomic_write_json(
        base / "summary_index.json",
        {"meta": {"test_size": 24, "detail_chunk_size": chunk_size}, "resources": summary},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _percentile(values: List[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _timed_ms(callable_obj) -> tuple[Any, float]:
    started = time.perf_counter()
    value = callable_obj()
    return value, (time.perf_counter() - started) * 1000.0


def run_benchmark(args: argparse.Namespace) -> Dict[str, Any]:
    if args.resources <= 0 or args.points <= 24 or args.samples <= 0:
        raise ValueError("resources/samples must be positive and points must be greater than 24")
    sample_count = min(args.resources, args.samples)
    index = pd.date_range("2026-01-01", periods=args.points, freq="5min")
    series = pd.Series([0.5] * args.points, index=index)
    resource_ids = [f"vm-benchmark-{idx:06d}" for idx in range(args.resources)]

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        write_raw_resource_dataset(
            base,
            [_resource(resource_id, series) for resource_id in resource_ids],
            freq="5min",
        )
        _write_forecast_artifacts(base, resource_ids)
        store = _SingleForecastStore(
            AppConfig(out_dir=str(base)),
            GenerationConfig(detail_history_points_default=1000, detail_history_points_max=10000),
        )
        stride = max(1, args.resources // sample_count)
        targets = resource_ids[::stride][:sample_count]

        metadata_ms: List[float] = []
        chart_ms: List[float] = []
        max_training_points = 0
        for resource_id in targets:
            detail, elapsed = _timed_ms(
                lambda rid=resource_id: store.get_resource_detail(rid, include_charts=False)
            )
            if not detail or detail.get("resource_id") != resource_id:
                raise RuntimeError(f"metadata detail missing for {resource_id}")
            metadata_ms.append(elapsed)

            charts, elapsed = _timed_ms(
                lambda rid=resource_id: store.get_resource_charts(
                    rid,
                    metric="cpu",
                    history_points=1000,
                )
            )
            block = (charts or {}).get("charts", {}).get("cpu", {})
            points = len(block.get("y_train", [])) if isinstance(block, dict) else 0
            if not points:
                raise RuntimeError(f"chart detail missing for {resource_id}")
            max_training_points = max(max_training_points, points)
            chart_ms.append(elapsed)

        result = {
            "resources": args.resources,
            "points_per_metric": args.points,
            "samples": sample_count,
            "metadata_ms": {
                "p50": round(_percentile(metadata_ms, 0.50), 3),
                "p95": round(_percentile(metadata_ms, 0.95), 3),
                "target_p95": args.metadata_p95_target_ms,
            },
            "chart_ms": {
                "p50": round(_percentile(chart_ms, 0.50), 3),
                "p95": round(_percentile(chart_ms, 0.95), 3),
                "target_p95": args.chart_p95_target_ms,
            },
            "max_training_points": max_training_points,
        }
        result["passed"] = (
            result["metadata_ms"]["p95"] <= args.metadata_p95_target_ms
            and result["chart_ms"]["p95"] <= args.chart_p95_target_ms
            and max_training_points <= 1000
        )
        return result


def main() -> int:
    result = run_benchmark(_parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
