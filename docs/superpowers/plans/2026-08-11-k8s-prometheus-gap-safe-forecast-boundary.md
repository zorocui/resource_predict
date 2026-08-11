# K8S Prometheus Gap-Safe Forecast Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make K8S Prometheus ingestion resilient to transient failures and sparse results while guaranteeing that future forecasts and the yellow chart area begin strictly after the last test timestamp.

**Architecture:** Add configurable HTTP retry and range-query chunking at the Prometheus client boundary, preserve upsert semantics for missing Workloads, and treat the configured K8S step as the authoritative forecast cadence. Normalize future indexes from the real test endpoint in the backend, then apply a defensive test-boundary filter and gap breaks in the ECharts adapter.

**Tech Stack:** Python 3, pandas, urllib, Flask runtime configuration, unittest/pytest, vanilla JavaScript, Node.js test runner, ECharts.

## Global Constraints

- K8S project terminology uses Workload; Pod is reserved for Kubernetes/Prometheus labels.
- K8S configured `step_seconds` is authoritative for forecast-window conversion and canonical future timestamps.
- The first future timestamp is exactly `last_test_timestamp + sample_interval`; every future timestamp is strictly later than the last test timestamp.
- The yellow area starts at the last valid test timestamp and ends at the last valid future prediction timestamp; current wall-clock time is irrelevant.
- Missing Workloads from one fetch are preserved; absence is not a deletion signal.
- Interpolate at most 3 consecutive missing sampling steps by default; never interpolate across larger gaps.
- Keep `rate_window` configurable and do not introduce a hardcoded CPU rate window.
- Preserve `reuse_backtest_model_for_future=True`; do not add a second full fit as the normal path.
- Use `apply_patch` for manual edits and keep UTF-8 source and Markdown intact.
- Run Python commands with `.\.venv\Scripts\python.exe`.
- Preserve unrelated `.codex_tmp/`, `.qoder/`, and other user changes.

---

## File Map

- `resource_predict/services/runtime_config.py`: defines and validates persisted runtime collection settings.
- `resource_predict/internal_settings.py`: exposes runtime collection settings to the provider and pipeline.
- `deploy/runtime_config.json`: checked-in defaults used by deployments.
- `static/js/index.js`: system-configuration form fields and payload serialization.
- `resource_predict/providers/k8s_prometheus.py`: Prometheus HTTP transport, range chunking, retry policy, bounded regularization, and Workload aggregation.
- `resource_predict/pipeline/windowing.py`: resolves test/future point counts and authoritative sample interval.
- `resource_predict/pipeline/_types.py`: transports canonical sample interval into workers.
- `resource_predict/pipeline/fit.py`: creates canonical future indexes after model prediction.
- `resource_predict/pipeline/run.py`: selects K8S frequency policy, records skipped predictions, and builds worker context.
- `resource_predict/pipeline/worker.py`: emits chart timing/data-quality metadata.
- `resource_predict/data/io.py`: carries forecast metadata into chart-detail responses.
- `static/js/charts.js`: clips invalid future points, builds the yellow area from the test boundary, and inserts visual gap breaks.
- `tests/test_system_config.py`: runtime-setting validation and persistence tests.
- `tests/test_k8s_workload_provider.py`: retry, chunking, merge, and bounded interpolation tests.
- `tests/test_forecast_windowing.py`: authoritative K8S frequency and sparse-window tests.
- `tests/test_forecast_optimizations.py`: canonical future-index tests with model reuse.
- `tests/test_raw_store.py`: missing-Workload preservation regression test.
- `tests/test_io.py`: chart metadata propagation tests.
- `tests/js/test_charts.mjs`: future-boundary and gap-rendering tests.
- `docs/configuration.md`, `docs/architecture.md`, `docs/api-reference.md`, `README.md`: operational and architecture documentation.

---

### Task 1: Add and expose ingestion reliability settings

**Files:**
- Modify: `resource_predict/services/runtime_config.py`
- Modify: `resource_predict/internal_settings.py`
- Modify: `deploy/runtime_config.json`
- Modify: `static/js/index.js`
- Test: `tests/test_system_config.py`

**Interfaces:**
- Produces: `CollectionConfig.range_query_chunk_hours: int`, `request_max_attempts: int`, `retry_backoff_seconds: float`, and `max_interpolation_gap_steps: int`.
- Produces: the same attributes through `settings.k8s_prometheus`.
- Consumes: existing `RuntimeConfigStore`, `_positive_int`, runtime JSON, and system-config form patterns.

- [ ] **Step 1: Write failing runtime configuration tests**

Import `json` and `write_runtime_config` in `tests/test_system_config.py`. Add tests that assert defaults, round-trip persistence, and field-specific rejection:

```python
def test_collection_reliability_defaults_and_roundtrip(self):
    runtime = runtime_config_to_dict(default_runtime_config())
    collection = runtime["collection"]
    self.assertEqual(collection["range_query_chunk_hours"], 24)
    self.assertEqual(collection["request_max_attempts"], 3)
    self.assertEqual(collection["retry_backoff_seconds"], 1.0)
    self.assertEqual(collection["max_interpolation_gap_steps"], 3)

    store = RuntimeConfigStore(default_runtime_config())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runtime.json"
        normalized = store.replace_payload(runtime)
        write_runtime_config(normalized, path)
        persisted = json.loads(path.read_text(encoding="utf-8"))
    self.assertEqual(persisted["collection"], collection)

def test_collection_reliability_values_must_be_positive(self):
    for field, value in (
        ("range_query_chunk_hours", 0),
        ("request_max_attempts", 0),
        ("retry_backoff_seconds", 0),
        ("max_interpolation_gap_steps", 0),
    ):
        payload = runtime_config_to_dict(default_runtime_config())
        payload["collection"][field] = value
        with self.subTest(field=field):
            with self.assertRaisesRegex(RuntimeConfigValidationError, field):
                RuntimeConfigStore(default_runtime_config()).replace_payload(payload)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_system_config.py -q
```

Expected: FAIL because the four collection fields do not exist or are not validated.

- [ ] **Step 3: Implement configuration fields and validation**

Extend `CollectionConfig` and runtime parsing with exact defaults:

```python
@dataclass(frozen=True)
class CollectionConfig:
    scheduled_update_enabled: bool = True
    scheduled_update_interval_minutes: int = 360
    history_days: int = 7
    step_seconds: int = 300
    rate_window: str = "5m"
    request_timeout_seconds: int = 300
    range_query_chunk_hours: int = 24
    request_max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    max_interpolation_gap_steps: int = 3
```

Parse integer fields with `_positive_int`. Parse `retry_backoff_seconds` with a positive-float helper that raises `RuntimeConfigValidationError("runtime.collection.retry_backoff_seconds", ...)` for non-numeric, zero, negative, or non-finite values.

Add matching fields to `K8SPrometheusConfig`, map them in `SettingsProxy.k8s_prometheus`, add the four values to `deploy/runtime_config.json`, render four numeric inputs in `renderCollectionConfig()`, and include them in the collection payload built by `collectRuntimeConfig()`.

- [ ] **Step 4: Run configuration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_system_config.py tests/test_forecast_config.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit configuration support**

```powershell
git add resource_predict/services/runtime_config.py resource_predict/internal_settings.py deploy/runtime_config.json static/js/index.js tests/test_system_config.py
git commit -m "feat: configure Prometheus ingestion resilience"
```

---

### Task 2: Add retry and range-query chunking to the Prometheus client

**Files:**
- Modify: `resource_predict/providers/k8s_prometheus.py`
- Test: `tests/test_k8s_workload_provider.py`

**Interfaces:**
- Consumes: Task 1 settings through `settings.k8s_prometheus`.
- Produces: `PrometheusClient(..., max_attempts: int, retry_backoff_seconds: float, range_query_chunk_hours: int)`.
- Produces: `_merge_matrix_results(chunks: Iterable[List[Dict[str, Any]]]) -> List[Dict[str, Any]]` keyed by the complete `metric` label mapping.

- [ ] **Step 1: Write failing retry tests**

Import `urllib.error` in the provider and test module. Add this concrete response helper to the test module, then patch `urllib.request.urlopen` and `time.sleep` to cover retryable and non-retryable failures:

```python
class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self._body

def test_prometheus_client_retries_503_then_succeeds(self):
    error = urllib.error.HTTPError("http://prom", 503, "busy", {}, None)
    response = _FakeResponse({"status": "success", "data": {"result": []}})
    client = provider.PrometheusClient(
        "http://prom", max_attempts=3, retry_backoff_seconds=0.25
    )
    with patch("urllib.request.urlopen", side_effect=[error, response]) as open_url:
        with patch("resource_predict.providers.k8s_prometheus.time.sleep") as sleep:
            self.assertEqual(client.query("up"), [])
    self.assertEqual(open_url.call_count, 2)
    sleep.assert_called_once_with(0.25)

def test_prometheus_client_does_not_retry_bad_request(self):
    error = urllib.error.HTTPError("http://prom", 400, "bad query", {}, None)
    client = provider.PrometheusClient("http://prom", max_attempts=3)
    with patch("urllib.request.urlopen", side_effect=error) as open_url:
        with self.assertRaises(urllib.error.HTTPError):
            client.query("bad")
    self.assertEqual(open_url.call_count, 1)
```

Also test `URLError`, `TimeoutError`, HTTP 429, and HTTP 500, asserting the delay sequence is `base`, `base * 2` and that authorization headers are never logged.

- [ ] **Step 2: Run retry tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_k8s_workload_provider.py -k "retry or bad_request" -q
```

Expected: FAIL because `PrometheusClient` has no retry configuration.

- [ ] **Step 3: Implement retry classification and exponential backoff**

Add fields to the frozen client dataclass and keep request construction unchanged. Implement helpers with these signatures:

```python
def _is_retryable_http_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599

def _retry_delay(base_seconds: float, attempt_index: int) -> float:
    return float(base_seconds) * (2 ** max(0, attempt_index))
```

Wrap only the URL open/read/JSON decode operation. Retry `urllib.error.URLError`, `TimeoutError`, `ConnectionError`, and retryable `HTTPError`; re-raise other `HTTPError` immediately. A Prometheus payload with `status != "success"` remains a semantic `RuntimeError` and is not retried.

- [ ] **Step 4: Write failing range chunk tests**

Use a recording client subclass or patch `_get` so a 49-hour range with a 24-hour chunk produces three calls. Assert the final `end` is preserved and overlapping timestamps are deduplicated:

```python
def test_query_range_chunks_and_merges_by_full_metric_labels(self):
    client = provider.PrometheusClient(
        "http://prom", range_query_chunk_hours=24
    )
    chunks = [
        {"result": [{"metric": {"pod": "a", "container": "app"}, "values": [[10, "1"], [20, "2"]]}]},
        {"result": [{"metric": {"pod": "a", "container": "app"}, "values": [[20, "3"], [30, "4"]]}]},
        {"result": [{"metric": {"pod": "a", "container": "app"}, "values": [[40, "5"]]}]},
    ]
    with patch.object(provider.PrometheusClient, "_get", side_effect=chunks) as get:
        rows = client.query_range("metric", start=0, end=49 * 3600, step=10)
    self.assertEqual(get.call_count, 3)
    self.assertEqual(rows[0]["values"], [[10, "1"], [20, "3"], [30, "4"], [40, "5"]])
```

Add a test where the second chunk exhausts retries and verify no partial rows are returned.

- [ ] **Step 5: Implement chunk boundaries and deterministic merge**

Split `[start, end]` into chunks no longer than `range_query_chunk_hours * 3600`. Begin every chunk after the first at `previous_end` so Prometheus boundary inclusion cannot create a hole. Merge matrix rows by `json.dumps(metric, sort_keys=True, separators=(",", ":"))`; merge each row's `values` by numeric timestamp, keep the later chunk's sample on duplicates, and return rows sorted by metric key with samples sorted by timestamp.

When `end <= start` or the total range is no longer than one chunk, preserve one-request behavior.

- [ ] **Step 6: Pass Task 1 settings into all clients and run provider tests**

Construct clients in `_fetch_target()` and `_diagnose_target()` with:

```python
PrometheusClient(
    base_url=target.prometheus_url,
    bearer_token=target.bearer_token,
    basic_auth=target.basic_auth,
    timeout_seconds=int(target.request_timeout_seconds),
    max_attempts=int(settings.k8s_prometheus.request_max_attempts),
    retry_backoff_seconds=float(settings.k8s_prometheus.retry_backoff_seconds),
    range_query_chunk_hours=int(settings.k8s_prometheus.range_query_chunk_hours),
)
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_k8s_workload_provider.py tests/test_cluster_configs.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the transport reliability change**

```powershell
git add resource_predict/providers/k8s_prometheus.py tests/test_k8s_workload_provider.py
git commit -m "feat: retry and chunk Prometheus queries"
```

---

### Task 3: Bound interpolation and preserve missing Workloads

**Files:**
- Modify: `resource_predict/providers/k8s_prometheus.py`
- Modify: `resource_predict/data/updater.py` only if the preservation regression exposes a defect
- Test: `tests/test_k8s_workload_provider.py`
- Test: `tests/test_raw_store.py`
- Test: `tests/test_cluster_configs.py`

**Interfaces:**
- Consumes: `settings.k8s_prometheus.max_interpolation_gap_steps` from Task 1.
- Produces: `_regularize_series(series: pd.Series, step_seconds: int, max_gap_steps: int) -> pd.Series` that interpolates only bounded interior gaps and returns remaining valid observations without fabricating large-gap samples.
- Preserves: existing `run_upsert_with_data()` resource-ID scoped read/write and partial prediction behavior.

- [ ] **Step 1: Write failing bounded-interpolation tests**

```python
def test_regularize_series_fills_short_gap_but_not_large_gap(self):
    idx = pd.to_datetime([
        "2026-08-01 00:00", "2026-08-01 00:10",
        "2026-08-01 01:00", "2026-08-01 01:05",
    ])
    series = pd.Series([0.0, 2.0, 10.0, 11.0], index=idx)
    result = provider._regularize_series(series, 300, max_gap_steps=3)
    self.assertEqual(result.loc[pd.Timestamp("2026-08-01 00:05")], 1.0)
    self.assertNotIn(pd.Timestamp("2026-08-01 00:15"), result.index)
    self.assertNotIn(pd.Timestamp("2026-08-01 00:55"), result.index)
```

Verify `_data_quality()` still reports the original large gap rather than the post-interpolation series.

- [ ] **Step 2: Run interpolation tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_k8s_workload_provider.py -k "regularize or data_quality" -q
```

Expected: FAIL because `_regularize_series` currently fills every missing value.

- [ ] **Step 3: Implement bounded interpolation for Workload and container series**

Use resampling plus a bounded interior interpolation:

```python
def _regularize_series(
    series: pd.Series,
    step_seconds: int,
    max_gap_steps: int,
) -> pd.Series:
    ordered = series.sort_index()
    if ordered.empty:
        return ordered
    rule = f"{max(1, int(step_seconds))}s"
    resampled = ordered.resample(rule).mean()
    filled = resampled.interpolate(
        method="time",
        limit=max(0, int(max_gap_steps)),
        limit_area="inside",
    )
    return filled.dropna()
```

Pass the configured limit to all four Workload series and all container series. Compute `_data_quality` before regularization so gaps remain observable.

- [ ] **Step 4: Write a missing-Workload preservation regression test**

Create two K8S resources in a temporary raw store, upsert data for only one, patch prediction to return only the updated resource, then assert both IDs remain in `raw_index.json` and in merged forecast output. The critical assertions are:

```python
self.assertEqual(set(RawResourceStore(base).resource_ids()), {rid_a, rid_b})
self.assertEqual(RawResourceStore(base).get(rid_b)["cpu_limit"].tolist(), original_values)
self.assertIn(rid_b, {item["resource_id"] for item in load_existing_forecast_items(base)})
```

Also retain the existing all-clusters-failed test asserting `run_upsert_with_data` is not called.

- [ ] **Step 5: Run merge and ingestion tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_raw_store.py tests/test_cluster_configs.py tests/test_k8s_workload_provider.py -q
```

Expected: PASS. If the new preservation test passes without editing `updater.py`, leave that file unchanged.

- [ ] **Step 6: Commit bounded gaps and preservation coverage**

```powershell
git add resource_predict/providers/k8s_prometheus.py tests/test_k8s_workload_provider.py tests/test_raw_store.py tests/test_cluster_configs.py
git add resource_predict/data/updater.py
git commit -m "fix: preserve workloads across sparse Prometheus pulls"
```

Before committing, omit `resource_predict/data/updater.py` from staging if it did not require a change.

---

### Task 4: Make K8S cadence authoritative and select a recent contiguous segment

**Files:**
- Modify: `resource_predict/pipeline/windowing.py`
- Modify: `resource_predict/pipeline/prepare.py`
- Modify: `resource_predict/pipeline/run.py`
- Modify: `resource_predict/pipeline/write_outputs.py`
- Test: `tests/test_forecast_windowing.py`

**Interfaces:**
- Produces: `resolve_forecast_window(..., fallback_freq: Optional[str], prefer_fallback_freq: bool = False) -> ForecastWindow`.
- Produces: `recent_contiguous_segment(series: pd.Series, sample_interval_seconds: float, max_gap_steps: int) -> pd.Series`.
- Consumes: Task 1 `max_interpolation_gap_steps` and the K8S raw metadata frequency.

- [ ] **Step 1: Write failing authoritative-cadence tests**

```python
def test_sparse_workload_prefers_configured_frequency(self):
    idx = pd.to_datetime([
        "2026-08-01 00:00", "2026-08-01 00:05", "2026-08-03 00:00"
    ])
    item = {
        "resource_id": "k8s:a:ns:deployment:api",
        "resource_type": "k8s_workload",
        "cpu_limit": pd.Series([0.1, 0.2, 0.3], index=idx),
    }
    window = resolve_forecast_window(
        cfg=settings.generation,
        items=[item],
        explicit_test_size=None,
        explicit_future_steps=None,
        fallback_freq="300s",
        prefer_fallback_freq=True,
    )
    self.assertEqual(window.sample_interval_seconds, 300.0)
    self.assertEqual(window.test_size, 288)
    self.assertEqual(window.future_steps, 288)
```

Keep the existing VM test proving actual interval still wins when `prefer_fallback_freq=False`.

- [ ] **Step 2: Write failing contiguous-segment tests**

```python
def test_recent_contiguous_segment_starts_after_last_large_gap(self):
    idx = pd.to_datetime([
        "2026-08-01 00:00", "2026-08-01 00:05",
        "2026-08-03 00:00", "2026-08-03 00:05", "2026-08-03 00:10",
    ])
    series = pd.Series(range(5), index=idx, dtype=float)
    result = recent_contiguous_segment(series, 300.0, max_gap_steps=3)
    self.assertEqual(result.index[0], pd.Timestamp("2026-08-03 00:00"))
    self.assertEqual(result.tolist(), [2.0, 3.0, 4.0])
```

Add a multi-metric Workload fixture showing that each top-level metric and each container metric is trimmed to its own recent segment before short-series eligibility is checked.

- [ ] **Step 3: Run focused windowing tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_forecast_windowing.py -k "sparse_workload or contiguous_segment" -q
```

Expected: FAIL because fallback frequency is not authoritative and no contiguous-segment helper exists.

- [ ] **Step 4: Implement cadence preference and segment selection**

In `resolve_forecast_window`, calculate both values and choose explicitly:

```python
observed_seconds = infer_sample_interval_seconds(index)
fallback_seconds = _fixed_frequency_seconds(fallback_freq)
sample_seconds = (
    fallback_seconds
    if prefer_fallback_freq and fallback_seconds is not None
    else observed_seconds if observed_seconds is not None else fallback_seconds
)
```

Implement `recent_contiguous_segment` by sorting/deduplicating the index, computing positive adjacent differences, and slicing after the last difference greater than `sample_interval_seconds * (max_gap_steps + 1)`. Do not interpolate in this helper.

In `generate_forecasts`, determine the resource family with `resource_family_for_items(prepared_data)` before resolving the window and pass `prefer_fallback_freq=True` for Workloads. Commit or read the raw snapshot before creating a forecast-only trimmed copy, then trim that copy before the `min_len <= test_size` gate so raw-store history remains unchanged. Apply the same trimming to `container_metrics`.

For each metric, extend its `data_quality` entry with `recent_contiguous_points`, `recent_contiguous_span_hours`, `data_end_ms`, and `prediction_skipped`. Pass skipped resource/metric reasons to `write_prediction_outputs()` and store them in manifest/report metadata so a quality-based skip remains observable when the previous detail artifact is retained.

Use this exact metadata shape:

```python
quality.update({
    "recent_contiguous_points": int(len(segment)),
    "recent_contiguous_span_hours": round(
        max(0.0, (segment.index[-1] - segment.index[0]).total_seconds()) / 3600.0,
        2,
    ) if len(segment) >= 2 else 0.0,
    "data_end_ms": int(series.index.max().value // 1_000_000),
    "prediction_skipped": len(segment) <= test_size,
})
```

Store skip entries as `{"resource_id": rid, "metric": metric, "reason": "recent_contiguous_segment_too_short"}` under `meta.prediction_skips`.

- [ ] **Step 5: Run all forecast-window tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_forecast_windowing.py tests/test_pipeline_worker.py -q
```

Expected: PASS, including existing short-series frequency fallback tests.

- [ ] **Step 6: Commit cadence and segment selection**

```powershell
git add resource_predict/pipeline/windowing.py resource_predict/pipeline/prepare.py resource_predict/pipeline/run.py resource_predict/pipeline/write_outputs.py tests/test_forecast_windowing.py
git commit -m "fix: use configured cadence for sparse workloads"
```

---

### Task 5: Canonicalize backend future timestamps after the test boundary

**Files:**
- Modify: `resource_predict/pipeline/_types.py`
- Modify: `resource_predict/pipeline/run.py`
- Modify: `resource_predict/pipeline/fit.py`
- Modify: `resource_predict/pipeline/worker.py`
- Modify: `resource_predict/data/io.py`
- Test: `tests/test_forecast_optimizations.py`
- Test: `tests/test_pipeline_worker.py`
- Test: `tests/test_io.py`

**Interfaces:**
- Produces: `WorkerContext.sample_interval_seconds: Optional[float]` and `WorkerContext.max_interpolation_gap_steps: int`.
- Produces: `canonical_future_index(test_index: pd.DatetimeIndex, steps: int, sample_interval_seconds: Optional[float]) -> pd.DatetimeIndex`.
- Produces chart fields: `test_end_ms: int`, `sample_interval_seconds: float`, and `max_interpolation_gap_steps: int`.
- Consumes: Task 4 `ForecastWindow.sample_interval_seconds`.

- [ ] **Step 1: Write failing canonical-index tests**

Extend `_ctx()` with `sample_interval_seconds=300.0` and add an irregular test index:

```python
def test_reused_model_future_index_starts_after_real_test_end(self):
    index = pd.to_datetime([
        "2026-08-01 00:00", "2026-08-01 00:05", "2026-08-01 00:10",
        "2026-08-02 00:00", "2026-08-03 04:00", "2026-08-09 04:00",
    ])
    y_full = pd.Series(range(6), index=index, dtype=float)
    y_train, y_test = y_full.iloc[:-3], y_full.iloc[-3:]

    def fake_forecast(_method, train, steps):
        wrong = pd.date_range(train.index[-1], periods=steps + 1, freq="5min")[1:]
        return ForecastResult(pd.Series(range(steps), index=wrong, dtype=float), 0.1)

    with patch("resource_predict.pipeline.fit.forecast_by_method", fake_forecast):
        _preds, _metrics, _best, future, _timing, _diagnostics = fit_one_metric(
            y_train, y_test, y_full,
            ctx=_ctx({"reuse_backtest_model_for_future": True}),
        )
    expected = pd.date_range(y_test.index[-1] + pd.Timedelta(minutes=5), periods=2, freq="5min")
    self.assertTrue(future["rolling_mean"].index.equals(expected))
```

Add a second test for `reuse_backtest_model_for_future=False` so all paths share the canonical index.

- [ ] **Step 2: Run optimization tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_forecast_optimizations.py -k "future_index" -q
```

Expected: FAIL because future series retain model-generated timestamps.

- [ ] **Step 3: Implement the canonical index and apply it before ensemble creation**

Add `sample_interval_seconds` and `max_interpolation_gap_steps` to `WorkerContext.__slots__` and constructor. Pass `window.sample_interval_seconds` and `settings.k8s_prometheus.max_interpolation_gap_steps` from `run.py`; use the default `3` for non-K8S callers and test helpers.

Implement:

```python
def canonical_future_index(
    test_index: pd.DatetimeIndex,
    steps: int,
    sample_interval_seconds: Optional[float],
) -> pd.DatetimeIndex:
    if not isinstance(test_index, pd.DatetimeIndex) or test_index.empty:
        raise ValueError("cannot build future index without a test endpoint")
    seconds = float(sample_interval_seconds or 0)
    if not np.isfinite(seconds) or seconds <= 0:
        raise ValueError("cannot build future index without a positive sample interval")
    start = test_index.max() + pd.Timedelta(seconds=seconds)
    return pd.date_range(start=start, periods=int(steps), freq=pd.Timedelta(seconds=seconds))
```

After all per-model future values are available and before `ensemble_series`, replace every future series index with the same canonical index. Validate `len(series) == ctx.future_steps`; a mismatch records a method failure and removes that future series rather than emitting ambiguous timestamps.

- [ ] **Step 4: Emit and propagate chart timing metadata**

In both Workload-level and container chart blocks in `worker.py`, add:

```python
"test_end_ms": int(y_test.index.max().value // 1_000_000),
"sample_interval_seconds": float(ctx.sample_interval_seconds),
"max_interpolation_gap_steps": int(ctx.max_interpolation_gap_steps),
```

Update `merge_charts_into_detail()` and `_merge_container_charts()` to copy these fields alongside `x_pred_ms`, and add assertions in `tests/test_io.py`.

- [ ] **Step 5: Run backend prediction and IO tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_forecast_optimizations.py tests/test_pipeline_worker.py tests/test_io.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit canonical future timing**

```powershell
git add resource_predict/pipeline/_types.py resource_predict/pipeline/run.py resource_predict/pipeline/fit.py resource_predict/pipeline/worker.py resource_predict/data/io.py tests/test_forecast_optimizations.py tests/test_pipeline_worker.py tests/test_io.py
git commit -m "fix: anchor future forecasts after test data"
```

---

### Task 6: Enforce the test boundary and gap breaks in ECharts

**Files:**
- Modify: `static/js/charts.js`
- Test: `tests/js/test_charts.mjs`

**Interfaces:**
- Produces: `lastValidTimestamp(xValues: unknown[]) -> number | null`.
- Produces: `futureForecastRange(xPredFuture, predsFuture, testEndMs) -> {startMs, endMs} | null`.
- Produces: `futurePairsAfterTest(xValues, yValues, testEndMs) -> Array<[number, number]>`.
- Produces: `insertGapBreaks(pairs, sampleIntervalSeconds, maxGapSteps) -> Array<[number, number | null]>`.
- Consumes: Task 5 `chartData.test_end_ms` and `chartData.sample_interval_seconds`, with `x_test_ms` fallback for old artifacts.

- [ ] **Step 1: Replace the existing future-range tests with failing test-boundary tests**

Use the real test endpoint as the area start, including the single-future-point case:

```javascript
test("future range starts at test end and ignores predictions inside test data", () => {
  const testEnd = times[3];
  const result = futureForecastRange(
    [times[2], times[3], times[4], times[5]],
    { rolling_mean: [0.1, 0.2, 0.3, 0.4] },
    testEnd
  );
  assert.deepEqual(result, { startMs: testEnd, endMs: times[5] });
});

test("one future point after test end creates a nonzero forecast area", () => {
  assert.deepEqual(
    futureForecastRange([times[4]], { rolling_mean: [0.3] }, times[3]),
    { startMs: times[3], endMs: times[4] }
  );
});

test("future range is absent when every prediction is inside the test interval", () => {
  assert.equal(
    futureForecastRange([times[2], times[3]], { rolling_mean: [0.2, 0.3] }, times[3]),
    null
  );
});
```

- [ ] **Step 2: Add failing chart integration and gap-break tests**

Construct chart data where `x_pred_ms` overlaps `x_test_ms`. Assert every future pair in the model series is later than `testEnd`, and assert `markArea.data` is `[[{xAxis: testEnd}, {xAxis: lastFuture}]]`.

Add:

```javascript
test("gap breaks prevent history and test lines from crossing large outages", () => {
  const result = insertGapBreaks(
    [[times[0], 0.1], [times[1], 0.2], [times[7], 0.3]],
    3600,
    3
  );
  assert.deepEqual(result, [
    [times[0], 0.1],
    [times[1], 0.2],
    [times[1] + HOUR, null],
    [times[7] - HOUR, null],
    [times[7], 0.3],
  ]);
});
```

- [ ] **Step 3: Run JavaScript tests and verify failure**

Run:

```powershell
node --test tests/js/test_charts.mjs
```

Expected: FAIL because the helpers do not accept a test boundary or insert gaps.

- [ ] **Step 4: Implement shared boundary filtering**

Resolve the boundary with `normalizeTsMs(chartData.test_end_ms)` when valid, otherwise use the last valid `x_test_ms` value. `futurePairsAfterTest` must call `toPairs` and retain only pairs with `timestamp > testEndMs`.

Change `futureForecastRange` to scan only valid model points later than `testEndMs`, return `{startMs: testEndMs, endMs: latestFuture}` when `latestFuture > testEndMs`, and return `null` otherwise.

When composing each model series, use `futurePairsAfterTest` for `preds_future` instead of concatenating raw future pairs. Keep test prediction points unchanged.

- [ ] **Step 5: Implement visual gap breaks**

`insertGapBreaks` compares adjacent valid timestamps. If the difference exceeds `sampleIntervalSeconds * (maxGapSteps + 1) * 1000`, insert two null-valued points one expected interval after the left point and one expected interval before the right point. Avoid duplicate break timestamps when the gap is exactly two intervals.

Apply gap breaks to history and test series only. Set `connectNulls: false`. Read `max_interpolation_gap_steps` from a new chart field if supplied; otherwise use `3` for old artifacts.

- [ ] **Step 6: Run JavaScript tests**

Run:

```powershell
node --test tests/js/test_charts.mjs
```

Expected: all tests PASS.

- [ ] **Step 7: Commit frontend boundary protection**

```powershell
git add static/js/charts.js tests/js/test_charts.mjs
git commit -m "fix: keep forecast area after test data"
```

---

### Task 7: Document behavior and run full regression verification

**Files:**
- Modify: `docs/configuration.md`
- Modify: `docs/architecture.md`
- Modify: `docs/api-reference.md`
- Modify: `README.md` only if its configuration summary or documentation index needs an updated link/field summary
- Test: full repository checks

**Interfaces:**
- Documents: exact Task 1 field names/defaults, retryable conditions, all-or-nothing per-query chunk behavior, missing-Workload preservation, bounded interpolation, canonical future timestamps, and yellow-area semantics.
- Consumes: all earlier tasks.

- [ ] **Step 1: Update configuration documentation**

Add the four fields to the K8S collection configuration JSON and table:

```json
{
  "range_query_chunk_hours": 24,
  "request_max_attempts": 3,
  "retry_backoff_seconds": 1.0,
  "max_interpolation_gap_steps": 3
}
```

State that retries apply to connection failures, timeouts, 429, and 5xx; range chunks are merged by full label set and timestamp; one exhausted chunk fails that cluster query.

- [ ] **Step 2: Update architecture and API documentation**

Document this data flow in `docs/architecture.md`:

```text
Prometheus range window
  -> 24h chunks
  -> per-request retry
  -> label/timestamp merge
  -> per-cluster atomic aggregation
  -> resource-ID scoped upsert
  -> recent contiguous forecast segment
  -> canonical future index after test_end_ms
  -> frontend test-boundary guard
```

In `docs/api-reference.md`, explain `partial_success`, retained missing Workloads, cluster errors, `test_end_ms`, `sample_interval_seconds`, and quality-based prediction skipping.

- [ ] **Step 3: Run focused test suites together**

Run:

```powershell
node --test tests/js/test_charts.mjs
.\.venv\Scripts\python.exe -m pytest tests/test_system_config.py tests/test_k8s_workload_provider.py tests/test_cluster_configs.py tests/test_raw_store.py tests/test_forecast_windowing.py tests/test_forecast_optimizations.py tests/test_pipeline_worker.py tests/test_io.py -q
```

Expected: all JavaScript and Python tests PASS.

- [ ] **Step 4: Run the required full regression suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\vulture.exe app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: every command exits 0; pytest reports no failures.

- [ ] **Step 5: Remove project Python caches created by verification**

First list only cache directories outside `.venv` and verify every resolved path is under the repository root. Then remove those exact directories with native PowerShell:

```powershell
$repoRoot = (Resolve-Path '.').Path
$cacheDirs = Get-ChildItem -Path $repoRoot -Recurse -Directory -Filter '__pycache__' |
  Where-Object { $_.FullName -notlike "$repoRoot\.venv\*" }
$cacheDirs | ForEach-Object {
  if (-not $_.FullName.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove cache outside repository: $($_.FullName)"
  }
}
$cacheDirs | Remove-Item -Recurse -Force
```

Expected: only project `__pycache__` directories outside `.venv` are removed.

- [ ] **Step 6: Review final diff and commit documentation**

Run:

```powershell
git status --short
git diff --check
git diff --stat
```

Verify no `.codex_tmp/`, `.qoder/`, `.venv/`, generated outputs, or unrelated user files are staged. Then commit:

```powershell
git add docs/configuration.md docs/architecture.md docs/api-reference.md
git add README.md
git commit -m "docs: explain gap-safe Prometheus forecasting"
```

Omit `README.md` from staging if no README change was necessary.

---

## Final Acceptance Checklist

- [ ] A 7-day Prometheus range is chunked and transient failures retry without leaking credentials.
- [ ] One exhausted chunk rejects that cluster query instead of merging a partial time range.
- [ ] Successful clusters still update when another cluster fails.
- [ ] Missing Workloads remain in raw storage and existing forecast outputs.
- [ ] Large gaps remain visible to data-quality logic and are not fully interpolated.
- [ ] K8S 5-minute configuration remains authoritative despite sparse timestamps.
- [ ] Low-quality recent segments skip recalculation and retain old prediction artifacts.
- [ ] Every future prediction timestamp is later than the real test endpoint.
- [ ] The yellow area starts at the test endpoint and never covers test data.
- [ ] History and test lines break across large outages.
- [ ] Config, architecture, API, and quick-start documentation are consistent.
- [ ] JavaScript tests and the full Python regression suite pass.
