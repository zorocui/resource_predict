"""Small, per-curve summaries of the latest prediction run's independent test."""
import json
import math
import time
from pathlib import Path

from resource_predict.data.io import atomic_write_json
from resource_predict.core.accuracy import RATIO_MODES, tolerance_hit
from resource_predict.resource_types import resource_type_of

FILENAME = "forecast_accuracy_summary.json"


def write_accuracy_summary(directory, items, *, replace_resource_ids=None):
    rows = []
    for item in items:
        kind = resource_type_of(item)
        containers = item.get("container_charts_forecast", {})
        for curve in item.get("_accuracy_holdout", []):
            container, metric = curve["container"], curve["metric"]
            # Prefer container detail over the overlapping Workload aggregate.
            if kind == "k8s_workload" and containers and not container:
                continue
            chart = (containers.get(container, {}).get(metric, {}) if container
                     else item.get("charts_forecast", {}).get(metric, {}))
            if curve["model"] != chart.get("best_method"):
                continue
            evaluation = curve.get("evaluation", {})
            mode = (item.get("container_metric_modes", {}).get(container, {}).get(metric) if container
                    else item.get("spec", {}).get(f"{metric}_metric_mode"))
            # 缺少基线时同名指标存的是核数/GiB，不能按利用率计入准确率。
            ratio = kind == "openstack_vm" or mode in RATIO_MODES
            valid = hits = invalid = 0
            error_sum = 0.0
            timestamps = curve["x_test_ms"]
            if len(timestamps) != len(curve["actual"]) or len(timestamps) != len(curve["yhat"]):
                raise ValueError("unaligned accuracy test curve")
            independent = (evaluation.get("role") == "independent_test"
                           and type(evaluation.get("test_train_end_ms")) is int
                           and all(t > evaluation["test_train_end_ms"] for t in timestamps))
            for actual, predicted in zip(curve["actual"], curve["yhat"]):
                if not independent or not all(type(v) in (float, int) and math.isfinite(v) for v in (actual, predicted)):
                    invalid += 1
                    continue
                error = abs(predicted-actual)
                valid += 1
                hits += int(ratio and tolerance_hit(actual, predicted))
                error_sum += error * (100 if ratio else 1)
            rows.append(dict(resource_id=item["resource_id"], resource_type=kind,
                             container=container, metric=metric, model=curve["model"],
                             valid_points=valid, invalid_points=invalid, hit_points=hits if ratio else None,
                             accuracy=hits/valid if valid and ratio else None,
                             mae=error_sum/valid if valid else None,
                             unit="百分点" if ratio else mode or "未知单位",
                             test_start_ms=min(timestamps) if timestamps else None,
                             test_end_ms=max(timestamps) if timestamps else None))
    payload = dict(version=2, generated_at_ms=int(time.time()*1000), rows=rows)
    if replace_resource_ids is not None:
        for row in rows:
            row["generated_at_ms"] = payload["generated_at_ms"]
        path = Path(directory) / FILENAME
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("version") == 2:
                payload["rows"] = [row for row in previous["rows"] if row.get("resource_id") not in replace_resource_ids] + rows
                payload["mixed_prediction_runs"] = True
                payload["full_run_generated_at_ms"] = previous.get("full_run_generated_at_ms", previous.get("generated_at_ms"))
    atomic_write_json(Path(directory)/FILENAME, payload, ensure_ascii=False, separators=(",", ":"))
    return payload


def read_accuracy_summary(directories):
    rows, runs, needs_regeneration = [], [], []
    for directory in directories:
        path = Path(directory)/FILENAME
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") not in {1, 2} or not isinstance(payload.get("rows"), list):
            raise ValueError("invalid accuracy summary")
        if payload["version"] == 1:
            needs_regeneration.append(Path(directory).name)
            continue
        runs.append(dict(scope=Path(directory).name, generated_at_ms=payload["generated_at_ms"],
                         mixed_prediction_runs=bool(payload.get("mixed_prediction_runs"))))
        rows.extend(payload["rows"])
    eligible = [row for row in rows if row["hit_points"] is not None]
    valid = sum(row["valid_points"] for row in eligible)
    hits = sum(row["hit_points"] for row in eligible)
    return dict(source="independent_test", accuracy=hits/valid if valid else None,
                valid_points=valid, hit_points=hits,
                resource_count=len({(r["resource_type"], r["resource_id"]) for r in eligible if r["valid_points"]}),
                invalid_points=sum(r["invalid_points"] for r in rows),
                absolute_unit_points=sum(r["valid_points"] for r in rows if r["hit_points"] is None),
                rows=rows, runs=runs, needs_regeneration=needs_regeneration)
