# Raw Monitoring Data 30-Day Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every locally persisted VM, K8S Workload, and K8S container monitoring series within a configurable 30-day observation window.

**Architecture:** Extend the unified runtime collection configuration with `retention_days`. Add one timestamp-based trimming helper in the updater and apply it after normalization/merge for both new and existing aggregate and container series; the existing content-addressed writer performs atomic replacement and orphan cleanup.

**Tech Stack:** Python 3, pandas, dataclasses, unittest/pytest, JSON configuration.

## Global Constraints

- Default `collection.retention_days` is `30` and must be a positive non-boolean integer.
- Retention is calculated independently for each series from its newest valid timestamp.
- Samples exactly on `latest_timestamp - retention_days` are retained.
- `collection.history_days` remains `7` and continues to mean Prometheus full-fetch range only.
- K8S Workload aggregate and `container_metrics` series must both be trimmed.
- Failed clusters and absent Workloads keep their existing raw and prediction artifacts.
- Use `apply_patch` for text edits and UTF-8 for Chinese documentation.
- Run project Python commands through `.\.venv\Scripts\python.exe`.

---

### Task 1: Add the unified retention configuration

**Files:**
- Modify: `resource_predict/services/runtime_config.py`
- Modify: `deploy/runtime_config.json`
- Test: `tests/test_runtime_config.py`
- Test: `tests/test_system_config.py`

**Interfaces:**
- Produces: `CollectionConfig.retention_days: int` with default `30`.
- Produces: normalized payload field `runtime.collection.retention_days`.
- Consumes: existing `_positive_int(value, path)` validation helper.

- [ ] **Step 1: Write failing default and validation tests**

Add assertions equivalent to:

```python
def test_default_retention_is_thirty_days(self):
    config = default_runtime_config()
    self.assertEqual(config.collection.retention_days, 30)
    self.assertEqual(runtime_config_to_dict(config)["collection"]["retention_days"], 30)

def test_retention_days_must_be_positive_integer(self):
    for invalid in (0, -1, True, 30.5, "30"):
        payload = runtime_config_to_dict(default_runtime_config())
        payload["collection"]["retention_days"] = invalid
        with self.assertRaisesRegex(RuntimeConfigValidationError, "retention_days"):
            normalize_runtime_config(payload)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_config.py tests/test_system_config.py -q
```

Expected: failure because `retention_days` is absent or rejected as an unknown field.

- [ ] **Step 3: Implement the configuration field**

Extend `CollectionConfig` and its normalization:

```python
@dataclass(frozen=True)
class CollectionConfig:
    scheduled_update_enabled: bool = True
    scheduled_update_interval_minutes: int = 360
    history_days: int = 7
    retention_days: int = 30
    step_seconds: int = 600
    rate_window: str = "15m"
    request_timeout_seconds: int = 300
    range_query_chunk_hours: int = 24
    request_max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    max_interpolation_gap_steps: int = 3

collection = CollectionConfig(
    scheduled_update_enabled=_bool(c["scheduled_update_enabled"], "runtime.collection.scheduled_update_enabled"),
    scheduled_update_interval_minutes=_positive_int(c["scheduled_update_interval_minutes"], "runtime.collection.scheduled_update_interval_minutes"),
    history_days=_positive_int(c["history_days"], "runtime.collection.history_days"),
    retention_days=_positive_int(c["retention_days"], "runtime.collection.retention_days"),
    step_seconds=_positive_int(c["step_seconds"], "runtime.collection.step_seconds"),
    rate_window=rate_window,
    request_timeout_seconds=_positive_int(c["request_timeout_seconds"], "runtime.collection.request_timeout_seconds"),
    range_query_chunk_hours=_positive_int(c["range_query_chunk_hours"], "runtime.collection.range_query_chunk_hours"),
    request_max_attempts=_positive_int(c["request_max_attempts"], "runtime.collection.request_max_attempts"),
    retry_backoff_seconds=_positive_float(c["retry_backoff_seconds"], "runtime.collection.retry_backoff_seconds"),
    max_interpolation_gap_steps=_positive_int(c["max_interpolation_gap_steps"], "runtime.collection.max_interpolation_gap_steps"),
)
```

Add the deployed default:

```json
"history_days": 7,
"retention_days": 30,
"step_seconds": 600
```

- [ ] **Step 4: Run the focused tests and verify success**

Run:

```bash
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_config.py tests/test_system_config.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the configuration change**

```bash
git add resource_predict/services/runtime_config.py deploy/runtime_config.json tests/test_runtime_config.py tests/test_system_config.py
git commit -m "config: add raw monitoring retention window"
```

---

### Task 2: Trim all raw series to the configured time window

**Files:**
- Modify: `resource_predict/data/updater.py`
- Test: `tests/test_raw_store.py`

**Interfaces:**
- Produces: `_trim_series_to_retention(series: pd.Series, retention_days: int) -> pd.Series`.
- Produces: `_trim_resource_to_retention(resource: Dict[str, Any], retention_days: int) -> bool` returning whether any series changed.
- Consumes: `runtime_config_store.snapshot().collection.retention_days` from `resource_predict.services.runtime_config`.

- [ ] **Step 1: Write failing helper boundary tests**

Add tests that construct an irregular series with points before, exactly on, and after the cutoff:

```python
def test_trim_series_retains_exact_thirty_day_boundary(self):
    idx = pd.to_datetime([
        "2026-06-30T23:59:59Z",
        "2026-07-01T00:00:00Z",
        "2026-07-20T12:00:00Z",
        "2026-07-31T00:00:00Z",
    ]).tz_localize(None)
    series = pd.Series([0.1, 0.2, 0.3, 0.4], index=idx)
    trimmed = updater._trim_series_to_retention(series, 30)
    self.assertEqual(trimmed.index.tolist(), idx[1:].tolist())
```

Also verify a one-point series remains unchanged and invalid retention values raise `ValueError`.

- [ ] **Step 2: Run the helper test and verify failure**

Run:

```bash
.\.venv\Scripts\python.exe -m pytest tests/test_raw_store.py -q
```

Expected: failure because `_trim_series_to_retention` does not exist.

- [ ] **Step 3: Implement the focused trimming helpers**

Implement timestamp filtering without point-count assumptions:

```python
def _trim_series_to_retention(series: pd.Series, retention_days: int) -> pd.Series:
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days <= 0:
        raise ValueError("retention_days must be a positive integer")
    if series.empty:
        return series
    normalized = series.sort_index()
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    cutoff = normalized.index[-1] - pd.Timedelta(days=retention_days)
    return normalized[normalized.index >= cutoff]
```

Implement `_trim_resource_to_retention` by iterating `metric_names_for_resource(resource)` and every nested `container_metrics[container][metric]` pandas series. Assign only changed series and return a boolean change flag.

- [ ] **Step 4: Write failing integration tests for existing and new resources**

Create a raw resource whose aggregate and container series span 31 days, then upsert a newest sample. Assert the persisted resource:

```python
self.assertGreaterEqual(loaded["cpu"].index[0], loaded["cpu"].index[-1] - pd.Timedelta(days=30))
self.assertGreaterEqual(
    loaded["container_metrics"]["app"]["cpu_limit"].index[0],
    loaded["container_metrics"]["app"]["cpu_limit"].index[-1] - pd.Timedelta(days=30),
)
```

Add a second test that upserts a brand-new resource containing 31 days and makes the same assertions before its first persisted shard is accepted.

- [ ] **Step 5: Integrate trimming into the update path**

Import `runtime_config_store` and capture the configured retention once at `_do_update` entry:

```python
from resource_predict.services.runtime_config import runtime_config_store

retention_days = int(runtime_config_store.snapshot().collection.retention_days)
```

For each existing resource, call `_trim_resource_to_retention` after all aggregate/container merges and include retention-only changes in `updated_resource_ids`. For each newly built resource, trim before appending it to `prepared`.

Ensure `updated_metrics_by_resource` includes every aggregate metric whose retained series changed so prediction regeneration uses the trimmed raw input. Container-only trimming must still mark the resource changed and trigger resource prediction regeneration.

- [ ] **Step 6: Run updater/raw-store tests and verify success**

Run:

```bash
.\.venv\Scripts\python.exe -m pytest tests/test_raw_store.py tests/test_forecast_windowing.py tests/test_output_isolation.py -q
```

Expected: all selected tests pass, including aggregate, container, and new-resource retention cases.

- [ ] **Step 7: Commit the retention behavior**

```bash
git add resource_predict/data/updater.py tests/test_raw_store.py
git commit -m "feat: retain thirty days of raw monitoring data"
```

---

### Task 3: Document behavior and run the full regression suite

**Files:**
- Modify: `docs/configuration.md`
- Modify: `docs/architecture.md`

**Interfaces:**
- Documents: `collection.retention_days=30` as local per-series observation retention.
- Documents: `collection.history_days=7` as the independent Prometheus full-fetch range.

- [ ] **Step 1: Update configuration documentation**

Add `retention_days` to the collection JSON example and parameter table, and state:

```text
history_days controls the first/full Prometheus fetch window. retention_days
controls local raw persistence. Each aggregate and container series retains
samples from its own newest timestamp back through the configured number of days.
```

- [ ] **Step 2: Update architecture documentation**

Extend the raw update flow to include timestamp trimming before the atomic shard replacement. Explicitly note that an unavailable resource is not wall-clock deleted and keeps a bounded last-known observation window.

- [ ] **Step 3: Check documentation and code diffs**

Run:

```bash
git diff --check
rg -n "retention_days|30 天|30-day" deploy/runtime_config.json resource_predict docs tests
```

Expected: no whitespace errors; all configuration, implementation, test, and documentation references are present.

- [ ] **Step 4: Run the complete project verification**

Run the exact commands from `docs/development.md`, using the local virtual environment:

```bash
.\.venv\Scripts\python.exe -m compileall -q app.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m vulture app.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all commands exit zero and the complete test suite passes.

- [ ] **Step 5: Remove generated project caches**

Use a read-only listing to resolve project `__pycache__` directories outside `.venv`, verify each resolved path is inside the repository, then remove only those directories with native PowerShell `Remove-Item -LiteralPath ... -Recurse -Force`.

- [ ] **Step 6: Commit documentation and final verification changes**

```bash
git add docs/configuration.md docs/architecture.md
git commit -m "docs: explain raw monitoring retention"
```

- [ ] **Step 7: Confirm final repository state**

Run:

```bash
git status --short
git log -4 --oneline
```

Expected: no unintended changes; recent commits contain the design, configuration, retention implementation, and documentation updates.
