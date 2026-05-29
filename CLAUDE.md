# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt

# Generate demo forecast artifacts (both VM and K8S scopes)
python generate_forecasts.py
# Re-predict only (reads existing raw_data.json, never overwrites it)
python generate_forecasts.py predict

# Start the Flask web app (http://127.0.0.1:5000)
python app.py

# K8S Prometheus ingestion (requires K8S_PROMETHEUS_CLUSTERS or deploy/k8s_prometheus_clusters.json)
python ingest_k8s_workloads.py
python ingest_k8s_workloads.py --diagnose        # check connectivity without writing

# Artifact health check
python check_outputs.py

# Regression / lint suite (run all four in order)
python -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
python -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
python -m pytest -q

# Run a single test file or test
python -m pytest tests/test_forecasting.py -q
python -m pytest tests/test_forecasting.py::test_function_name -q
```

## Architecture

This is a Python/Flask system that ingests cloud resource metrics, runs time-series forecasting, and produces scaling recommendations for two resource families:

- **VM (OpenStack)**: predicts `cpu / memory / disk`, generates `openstack server resize` commands
- **K8S Workload**: predicts `cpu / memory`, generates `kubectl set resources` and `kubectl scale` commands

### Two-Scope Output Model

All forecast artifacts are physically isolated by scope:

```
outputs/vm/   ← VM resources (cpu/memory/disk)
outputs/k8s/  ← K8S workloads (cpu/memory only)
```

Each scope directory contains `raw_data.json`, `summary_index.json`, `manifest.json`, and `details/*.json`. `ForecastStore` (in `services/store/`) merges both scopes at read time for the API layer. The `scoped_out_dir()` / `split_items_by_scope()` helpers in `pipeline/output_paths.py` enforce this separation. Never mix scopes in a single `raw_data.json`.

### Pipeline Flow

```
Provider (mock / real / Prometheus)
  → build_prepared_data() [pipeline/prepare.py]
  → write_raw_dataset()   [data/io.py → outputs/<scope>/raw_data.json]
  → resolve_parallel_plan() [pipeline/plan.py — decides ThreadPoolExecutor sizing]
  → worker() per resource  [pipeline/worker.py]
      → fit_one_metric() per metric [pipeline/fit.py — runs all active models]
      → model_selection picks best candidate [pipeline/model_selection.py]
      → build_scaling_advice() or build_k8s_workload_advice() [core/decision.py / core/k8s_workload_decision.py]
  → write_prediction_outputs() [pipeline/write_outputs.py]
```

`WorkerContext` (`pipeline/_types.py`) is the read-only context passed to every worker. `FitResult` is the per-metric return structure. The pipeline uses `concurrent.futures.ThreadPoolExecutor` for resource-level parallelism, with optional inner metric-level parallelism controlled by `resolve_parallel_plan()`.

### Data Update / Upsert System (`data/updater.py`)

The updater supports both pull (scheduled background thread calling `IncrementalProvider`) and push (HTTP `POST /api/update-data` or `/api/upsert-data`) modes. Key threading primitives:

- `_update_exclusive` (Lock): serializes the entire "read raw → merge → write raw → re-predict" sequence across HTTP and scheduler threads
- `_lock` (Lock): protects the `_update_status` dict for thread-safe reads
- `_stop_event` (Event): signals the background scheduler thread to exit

`fail_if_busy=True` raises `UpdateBusyError` (mapped to HTTP 409) instead of blocking. After merging, the updater calls `generate_predictions_only()` with `resource_ids` to do partial re-prediction rather than a full pipeline run.

### Scaling Execution (`services/scaling/`)

`build_scaling_plan()` in `executor.py` produces a `ScalingPlan` dataclass containing shell commands. Commands are built with `shlex.quote()` for all user-controlled values — never concatenate unquoted strings. The `command_runner.py` module executes commands via SSH. `openstack_flavors.py` queries available flavors from the control node to select a resize target; if no suitable flavor exists, `allow_create_flavor=True` enables auto-creation.

K8S commands use `kubectl set resources` with per-container granularity and `kubectl scale` for replica changes. DaemonSet replica scaling is explicitly skipped with a warning.

### Decision Logic Split

- `core/decision.py` — VM decisions: compares predicted P95/mean/peak against `DecisionConfig` thresholds, recommends `scale_out`, `scale_in`, or `hold`, computes target spec (cpu_cores/memory_gb/disk_gb snapped to even cores)
- `core/k8s_workload_decision.py` — K8S decisions: namespace-aware policy tiers (`conservative` / `balanced` / `aggressive`), confirmation rounds before execution, produces target requests/limits/replicas

Both modules read thresholds from `settings.decision` (`settings.py` → `DecisionConfig` dataclass).

### Provider Interface

All data providers must return the same structure regardless of source:

```python
{
    "resource_id": str,
    "resource_type": "openstack_vm" | "k8s_workload",
    "spec": { "cluster": str, "instance_id": str, ... },
    "metrics": {
        "cpu":    { "timestamps": [int_ms, ...], "values": [float_0_to_1, ...] },
        "memory": { "timestamps": [...], "values": [...] },
        # "disk" for VM only
    }
}
```

The `IncrementalProvider` callable signature is `(prepared_resources: List[Dict], points_to_add: int) -> List[Dict]`. Custom providers are resolved via `incremental_provider_path` in `settings.update` using `"module:function"` notation.

### Configuration (`settings.py`)

All defaults live in frozen `@dataclass` config objects under the global `settings = Settings()` singleton. The hierarchy:

- `AppConfig` — Flask host/port, output directory, logging
- `GenerationConfig` — forecast window sizes per resource family, parallelism, pagination
- `ForecastConfig` — enabled models, Prophet hyperparameters, clip modes, anomaly routing
- `DecisionConfig` — scale-out/in thresholds, confirmation rounds, cooldown periods, namespace policy tiers
- `UpdateConfig` — background scheduler interval, sliding window toggle, custom provider path
- `K8SPrometheusConfig` — multi-cluster Prometheus targets, step size, auth, namespace filter

Runtime overrides can come from `deploy/forecast_config.json` (model toggles) and `deploy/clusters.json` (scaling cluster config). Sensitive files (`deploy/*.json`, `.env`) are gitignored.

### Resource Type Routing (`resource_types.py`)

`resource_type_of()` normalizes raw type strings to canonical names (`openstack_vm`, `k8s_workload`, `k8s_pod`). `metric_names_for_resource()` returns `("cpu", "memory", "disk")` for VMs and `("cpu", "memory")` for K8S workloads. Always use these helpers rather than hardcoding metric lists.

## Conventions

- The package is `resource_predict`; CLI scripts (`app.py`, `generate_forecasts.py`, etc.) stay at the project root and only parse arguments — all business logic lives inside the package
- All config dataclasses use `frozen=True`; mutate via replacement, not assignment
- New K8S-related code uses `workload` in naming; `pod` only appears as a Prometheus label or legacy compatibility
- Forecast artifacts are called `outputs` or `forecast artifacts`, never `images`
- Timestamps in the API/payload layer are millisecond Unix ints; internally pandas `DatetimeIndex` (tz-naive after conversion from UTC Prometheus data)
- `predict_only=True` must never modify `raw_data.json`; the `generate_forecasts.py` CLI verifies this with a SHA-256 check
- Comments and log messages are in Chinese; keep this convention when adding new ones
