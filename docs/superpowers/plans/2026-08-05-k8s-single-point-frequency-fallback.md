# K8S Single-Point Frequency Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent K8S automatic updates from failing when a newly fetched Workload has too few timestamps to infer the sampling frequency for duration-based forecast windows.

**Architecture:** Carry the configured Prometheus `step_seconds` through K8S ingest as an explicit raw-frequency hint. Forecast window resolution continues to prefer actual timestamp intervals and uses persisted raw frequency only when the series cannot provide an interval, allowing existing short-series skip behavior to run instead of aborting the whole update.

**Tech Stack:** Python 3.10, pandas, NumPy, unittest/pytest

## Global Constraints

- Actual timestamp intervals remain authoritative whenever at least two distinct timestamps exist.
- K8S automatic fetch uses the current page-managed `step_seconds`; do not hardcode a 5-minute frequency.
- A single-point Workload must not abort other resources in the same update.
- Frequency fallback converts the 24-hour Workload window to points but does not synthesize historical observations.
- VM, manual push, configured `rate_window`, container-granularity metrics, and existing scaling gates remain unchanged.
- Run project Python commands through `.\.venv\Scripts\python.exe` and remove project `__pycache__` directories outside `.venv` after verification.

---

### Task 1: Add duration-window frequency fallback

**Files:**
- Modify: `resource_predict/pipeline/windowing.py`
- Modify: `resource_predict/pipeline/run.py`
- Modify: `tests/test_forecast_windowing.py`

**Interfaces:**
- Changes: `resolve_forecast_window(..., fallback_freq: Optional[str] = None) -> ForecastWindow`.
- Produces: `_fixed_frequency_seconds(freq: Optional[str]) -> Optional[float]` for safe fixed-frequency conversion.
- Consumes: the existing `freq` loaded from raw metadata in `generate_forecasts`.

- [ ] **Step 1: Write failing fallback and precedence tests**

Add these scenarios to `ForecastWindowingTest`:

```python
def test_single_point_workload_uses_raw_frequency_fallback(self):
    item = {
        "resource_id": "k8s:cluster-a:ns:deployment:api",
        "resource_type": "k8s_workload",
        "cpu": series(1, "5min"),
    }
    window = resolve_forecast_window(
        cfg=settings.generation,
        items=[item],
        explicit_test_size=None,
        explicit_future_steps=None,
        fallback_freq="300s",
    )
    self.assertEqual(window.sample_interval_seconds, 300.0)
    self.assertEqual(window.test_size, 288)
    self.assertEqual(window.future_steps, 288)

def test_actual_interval_wins_over_frequency_fallback(self):
    item = {
        "resource_id": "k8s:cluster-a:ns:deployment:api",
        "resource_type": "k8s_workload",
        "cpu": series(100, "15min"),
    }
    window = resolve_forecast_window(
        cfg=settings.generation,
        items=[item],
        explicit_test_size=None,
        explicit_future_steps=None,
        fallback_freq="300s",
    )
    self.assertEqual(window.sample_interval_seconds, 900.0)
    self.assertEqual(window.test_size, 96)
```

Retain an assertion that a single point without fallback raises `时间序列频率未知`.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_forecast_windowing.py -q`

Expected: FAIL because `resolve_forecast_window` does not accept `fallback_freq`.

- [ ] **Step 3: Implement fixed-frequency fallback**

Add the optional argument to `resolve_forecast_window`. Keep the current call to `infer_sample_interval_seconds(index)` and only when it returns `None`, call `_fixed_frequency_seconds(fallback_freq)`.

Implement the helper as:

```python
def _fixed_frequency_seconds(freq: Optional[str]) -> Optional[float]:
    if not freq:
        return None
    try:
        offset = pd.tseries.frequencies.to_offset(str(freq))
        seconds = float(pd.Timedelta(offset).total_seconds())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None
```

In `generate_forecasts`, pass the already resolved `freq` value to `resolve_forecast_window(..., fallback_freq=freq)`. Do not change inference after window resolution or the short-resource skip logic.

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_forecast_windowing.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the window fallback**

```bash
git add resource_predict/pipeline/windowing.py resource_predict/pipeline/run.py tests/test_forecast_windowing.py
git commit -m "fix: fall back to raw frequency for short series"
```

### Task 2: Carry configured K8S frequency through automatic updates

**Files:**
- Modify: `resource_predict/data/updater.py`
- Modify: `resource_predict/services/k8s_ingest.py`
- Modify: `tests/test_forecast_windowing.py`
- Modify: `tests/test_cluster_configs.py`

**Interfaces:**
- Changes: `run_upsert_with_data(..., freq_hint: Optional[str] = None) -> Dict[str, Any]`.
- Changes: `_do_update(..., freq_hint: Optional[str] = None) -> Dict[str, Any]`.
- Consumes: `settings.k8s_prometheus.step_seconds` and passes `f"{step_seconds}s"`.

- [ ] **Step 1: Write failing raw metadata and ingest propagation tests**

Add a one-point K8S upsert test that patches `resource_predict.pipeline.generate_predictions_only`, calls:

```python
result = run_upsert_with_data(
    [k8s_item],
    out_dir=base,
    fail_if_busy=True,
    freq_hint="300s",
)
```

and asserts success plus `RawResourceStore(base).metadata()["freq"] == "300s"`.

Update the existing K8S ingest mock settings to include `step_seconds=600`, then assert:

```python
upsert.assert_called_once_with(
    items,
    fail_if_busy=True,
    out_dir=Path(tmp) / "k8s",
    freq_hint="600s",
)
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_forecast_windowing.py tests/test_cluster_configs.py -q`

Expected: FAIL because `run_upsert_with_data` lacks `freq_hint` and K8S ingest does not pass it.

- [ ] **Step 3: Implement the update frequency hint**

Add `freq_hint` to `run_upsert_with_data` and `_do_update`, and pass it through unchanged. In `_do_update`, after reading or initializing raw metadata, apply:

```python
if freq_hint:
    freq = str(freq_hint)
```

Before raw write, retain the existing inference block only when `not freq_hint`; this prevents a single-point series from replacing `300s` with the generic `h` fallback.

In `run_k8s_prometheus_upsert`, derive:

```python
step_seconds = max(1, int(getattr(settings.k8s_prometheus, "step_seconds", 300)))
freq_hint = f"{step_seconds}s"
```

and pass `freq_hint=freq_hint` to `run_upsert_with_data`.

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_forecast_windowing.py tests/test_cluster_configs.py -q`

Expected: PASS.

- [ ] **Step 5: Run full regression checks and clean caches**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app.py resource_predict tools tests
.\.venv\Scripts\python.exe -m pyflakes app.py resource_predict tools tests
.\.venv\Scripts\python.exe -m vulture app.py resource_predict tools tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/js/*.mjs
```

Expected: all commands exit 0. Remove project `__pycache__` directories outside `.venv` and preserve `.codex_tmp/` and `.qoder/`.

- [ ] **Step 6: Commit the K8S propagation fix**

```bash
git add resource_predict/data/updater.py resource_predict/services/k8s_ingest.py tests/test_forecast_windowing.py tests/test_cluster_configs.py
git commit -m "fix: preserve configured K8S sampling frequency"
```

