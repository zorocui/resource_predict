# Unified Runtime Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the oversized frozen `settings` singleton with bootstrap-only settings plus a validated, page-managed runtime configuration that immediately controls collection, forecasting, decisions, and K8S scheduling.

**Architecture:** Introduce an immutable `RuntimeConfig` snapshot and thread-safe store backed by `deploy/runtime_config.json`. Each task captures one snapshot and passes its prediction/decision/collection sections through the existing pipeline; a unified system-config service validates and saves runtime plus cluster configuration before switching the live snapshot. The existing configuration page becomes a four-section system configuration workspace and uses one API and one save action.

**Tech Stack:** Python 3.10 dataclasses, Flask, JSON with atomic `os.replace`, threading locks/events, vanilla JavaScript, Node.js built-in test runner, ECharts-compatible page markup

## Global Constraints

- `settings.py` retains only static/template/output/log/host/port/debug bootstrap settings.
- Only the 22 fields listed in the approved design are accepted as page-managed runtime fields.
- Runtime save takes effect immediately for new tasks; a task uses one immutable snapshot for its whole execution.
- K8S schedule changes wake and reconfigure one existing scheduler thread; never run two automatic scheduler threads.
- Keep VM/K8S scaling credentials in `deploy/clusters.json` and Prometheus credentials in `deploy/k8s_prometheus_clusters.json`.
- Preserve K8S container-granularity current specs, execution gates, configured `rate_window`, and fractional small-spec recommendations.
- Production scale is thousands to tens of thousands of resources; do not add per-resource config-file reads.
- Use UTF-8 and `apply_patch`; README command examples remain CentOS/Linux-facing bash.
- Run repository Python commands through `.\.venv\Scripts\python.exe`.
- Remove project `__pycache__` directories outside `.venv` after verification.

---

### Task 1: Add the validated immutable runtime configuration store

**Files:**
- Create: `resource_predict/services/runtime_config.py`
- Create: `tests/test_runtime_config.py`

**Interfaces:**
- Produces: `RuntimeConfigValidationError(field: str, message: str)`
- Produces: frozen `CollectionConfig`, `PredictionConfig`, `DecisionConfig`, and `RuntimeConfig`
- Produces: `default_runtime_config() -> RuntimeConfig`
- Produces: `normalize_runtime_config(payload: Any) -> RuntimeConfig`
- Produces: `runtime_config_to_dict(config: RuntimeConfig) -> dict[str, Any]`
- Produces: `load_runtime_config(path=RUNTIME_CONFIG_PATH, legacy_forecast_path=LEGACY_FORECAST_CONFIG_PATH) -> tuple[RuntimeConfig, list[str]]`
- Produces: `RuntimeConfigStore.snapshot() -> RuntimeConfig`, `replace(config: RuntimeConfig) -> None`, `replace_payload(payload: Any) -> RuntimeConfig`, and module singleton `runtime_config_store`
- Produces: `write_runtime_config(config: RuntimeConfig, path=RUNTIME_CONFIG_PATH) -> None`

- [ ] **Step 1: Write failing model, validation, migration, persistence, and concurrency tests**

Create tests with these concrete assertions:

```python
def test_defaults_expose_only_runtime_whitelist(self):
    payload = runtime_config_to_dict(default_runtime_config())
    self.assertEqual(set(payload), {"collection", "prediction", "decision"})
    self.assertEqual(payload["collection"]["rate_window"], "5m")
    self.assertNotIn("fail_fast", payload["collection"])

def test_unknown_field_reports_stable_path(self):
    with self.assertRaises(RuntimeConfigValidationError) as caught:
        normalize_runtime_config({"collection": {"unknown": 1}})
    self.assertEqual(caught.exception.field, "runtime.collection.unknown")

def test_invalid_ratio_does_not_replace_store_snapshot(self):
    store = RuntimeConfigStore(default_runtime_config())
    before = store.snapshot()
    with self.assertRaises(RuntimeConfigValidationError):
        store.replace_payload({"decision": {"scale_out_threshold": 1.1}})
    self.assertIs(store.snapshot(), before)

def test_legacy_forecast_values_are_used_only_without_runtime_file(self):
    with tempfile.TemporaryDirectory() as tmp:
        runtime_path = Path(tmp) / "runtime.json"
        legacy_path = Path(tmp) / "forecast.json"
        legacy_path.write_text(
            json.dumps({"enabled_methods": ["rolling_mean"], "enable_ensemble": True}),
            encoding="utf-8",
        )
        migrated, _warnings = load_runtime_config(runtime_path, legacy_path)
        self.assertEqual(migrated.prediction.enabled_methods, ("rolling_mean",))
        self.assertTrue(migrated.prediction.enable_ensemble)
        explicit = replace(
            default_runtime_config(),
            prediction=replace(
                default_runtime_config().prediction,
                enabled_methods=("seasonal_naive",),
                enable_ensemble=False,
            ),
        )
        write_runtime_config(explicit, runtime_path)
        loaded, _warnings = load_runtime_config(runtime_path, legacy_path)
        self.assertEqual(loaded, explicit)

def test_atomic_writer_roundtrips_utf8_json(self):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runtime.json"
        default = default_runtime_config()
        config = replace(
            default,
            decision=replace(default.decision, conservative_namespaces=("生产", "核心")),
        )
        write_runtime_config(config, path)
        loaded, warnings = load_runtime_config(path)
        self.assertEqual(warnings, [])
        self.assertEqual(loaded.decision.conservative_namespaces, ("生产", "核心"))
```

Also start reader threads while repeatedly replacing snapshots and assert every observed snapshot has either the complete old tuple or complete new tuple, never a mixed section.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_runtime_config.py`

Expected: collection fails because `resource_predict.services.runtime_config` does not exist.

- [ ] **Step 3: Implement models, exact whitelist validation, legacy fallback, atomic write, and locked store**

Use the approved defaults and structure. Validation must reject non-object sections, booleans used as numbers, ratios outside `0..1`, non-positive integer durations/timeouts, unsupported models/policy tiers, empty model selections, duplicate/blank namespaces, and duration strings that `pandas.Timedelta` cannot parse or that resolve to zero/non-positive values.

Implement atomic writes with this lifecycle:

```python
tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(tmp_path, path)
```

`RuntimeConfigStore.replace_payload()` normalizes before entering the lock; the lock only swaps a complete frozen snapshot. Loading a malformed file returns defaults plus a warning that contains the path but never credential contents.

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_runtime_config.py`

Expected: all runtime-config tests pass.

- [ ] **Step 5: Commit the runtime configuration core**

```bash
git add resource_predict/services/runtime_config.py tests/test_runtime_config.py
git commit -m "feat: add runtime configuration store"
```

### Task 2: Introduce bootstrap settings and relocate non-business implementation defaults

**Files:**
- Modify: `resource_predict/settings.py`
- Modify: `app.py`
- Modify: `check_outputs.py`
- Modify: `generate_forecasts.py`
- Modify: `resource_predict/logging_setup.py`
- Modify: `resource_predict/api/pages.py`
- Modify: `resource_predict/pipeline/constants.py`
- Modify: `resource_predict/pipeline/output_paths.py`
- Modify: `resource_predict/data/updater.py`
- Modify: `resource_predict/services/store/forecast_store.py`
- Modify: `resource_predict/services/update_history.py`
- Modify: `resource_predict/services/scaling/snapshot.py`
- Modify: `resource_predict/services/scaling/tasks.py`
- Modify: `benchmarks/resource_detail_benchmark.py`
- Modify: `tests/test_app_scheduler.py`
- Modify: `tests/test_forecast_store.py`
- Modify: `tests/test_output_isolation.py`
- Test: `tests/test_settings_boundary.py`

**Interfaces:**
- Produces: frozen `BootstrapSettings` and singleton `bootstrap_settings`
- Consumes: `runtime_config_store.snapshot()` from Task 1
- Produces internal constants for mock generation, storage chunking/cache/page sizes, update behavior, and action-state retention in the modules that consume them
- Temporarily preserves the old runtime dataclasses/singleton until Task 3 migrates their remaining consumers; Task 3 removes them completely

- [ ] **Step 1: Write a failing bootstrap-boundary test**

```python
def test_bootstrap_settings_have_exact_startup_fields(self):
    from resource_predict import settings as module
    self.assertEqual(
        set(dataclasses.asdict(module.bootstrap_settings)),
        {"static_folder", "template_folder", "out_dir", "log_file", "log_level", "log_console", "host", "port", "debug"},
    )
```

Add focused tests asserting `generate_forecasts.py` mock defaults remain 45 resources, 240 points, seed 1000, and `ForecastStore` retains detail chunk/cache/page defaults after the old config dataclasses disappear.

- [ ] **Step 2: Run boundary tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_settings_boundary.py tests/test_app_scheduler.py tests/test_forecast_store.py tests/test_output_isolation.py`

Expected: the boundary test fails because `bootstrap_settings` does not exist.

- [ ] **Step 3: Replace the old singleton and update bootstrap/storage/update consumers**

Add the bootstrap dataclass and singleton to `settings.py`:

```python
@dataclass(frozen=True)
class BootstrapSettings:
    static_folder: str = "static"
    template_folder: str = "templates"
    out_dir: str = "outputs"
    log_file: Optional[str] = "resource_predict.log"
    log_level: str = "INFO"
    log_console: bool = True
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False

bootstrap_settings = BootstrapSettings()
```

Migrate all `settings.app` consumers to `bootstrap_settings`. Move mock defaults to `generate_forecasts.py`; move raw-cache, detail-chunk, history-point and paging limits to named constants in their storage/API modules; move VM incremental-provider/sliding-window defaults into `data/updater.py`; move action-state retention to `pipeline/constants.py`. Update constructor tests to pass primitive overrides or focused local option objects instead of importing the implementation-oriented generation/app dataclasses. Leave the old forecast/decision/collection singleton sections in place only until Task 3 converts their consumers.

- [ ] **Step 4: Run focused boundary and storage tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_settings_boundary.py tests/test_app_scheduler.py tests/test_forecast_store.py tests/test_output_isolation.py tests/test_raw_store.py tests/test_update_history.py`

Expected: all selected tests pass and `rg "settings\.app" app.py resource_predict check_outputs.py generate_forecasts.py` returns no production matches.

- [ ] **Step 5: Commit the bootstrap boundary**

```bash
git add resource_predict/settings.py app.py check_outputs.py generate_forecasts.py resource_predict tests/test_settings_boundary.py tests/test_app_scheduler.py tests/test_forecast_store.py tests/test_output_isolation.py benchmarks/resource_detail_benchmark.py
git commit -m "refactor: separate bootstrap settings"
```

### Task 3: Make forecast and decision tasks consume one runtime snapshot

**Files:**
- Modify: `resource_predict/pipeline/_types.py`
- Modify: `resource_predict/pipeline/run.py`
- Modify: `resource_predict/pipeline/fit.py`
- Modify: `resource_predict/pipeline/worker.py`
- Modify: `resource_predict/pipeline/partial.py`
- Modify: `resource_predict/pipeline/windowing.py`
- Modify: `resource_predict/core/forecasting.py`
- Modify: `resource_predict/core/decision.py`
- Modify: `resource_predict/core/k8s_workload_decision.py`
- Modify: `resource_predict/utils.py`
- Modify: `resource_predict/services/urgency.py`
- Modify: `resource_predict/api/resources.py`
- Modify: `resource_predict/providers/k8s_prometheus.py`
- Modify: `resource_predict/services/k8s_ingest.py`
- Modify: `resource_predict/services/cluster_configs.py`
- Modify: `resource_predict/services/forecast_config.py`
- Modify: `resource_predict/settings.py`
- Modify: `tests/test_forecast_windowing.py`
- Modify: `tests/test_forecast_config.py`
- Modify: `tests/test_decision.py`
- Modify: `tests/test_k8s_workload_decision.py`
- Modify: `tests/test_k8s_workload_provider.py`
- Modify: `tests/test_urgency.py`
- Modify: `tests/test_settings_boundary.py`

**Interfaces:**
- Consumes: `RuntimeConfig` snapshot from Task 1
- Adds keyword parameter `runtime_config: RuntimeConfig | None = None` to the existing `generate_forecasts()` signature; `None` captures the store snapshot once
- Produces: worker context containing immutable `PredictionConfig` and `DecisionConfig`
- Adds keyword parameter `collection_config: CollectionConfig | None = None` to the existing `fetch_k8s_workload_prometheus_result()` signature
- Produces internal forecast/decision algorithm constants that are not serializable runtime fields

- [ ] **Step 1: Add failing snapshot-consistency and runtime-value tests**

Add tests with explicit runtime dataclass replacements:

```python
def test_forecast_window_uses_runtime_durations(self):
    prediction = replace(default_runtime_config().prediction, workload_future_duration="48h")
    window = resolve_forecast_window(cfg=prediction, items=k8s_items, explicit_test_size=None, explicit_future_steps=None)
    self.assertEqual(window.future_steps, 48 * samples_per_hour)

def test_pipeline_captures_runtime_snapshot_once(self):
    default = default_runtime_config()
    first = replace(
        default,
        prediction=replace(default.prediction, vm_future_duration="12h"),
    )
    with patch.object(runtime_config_store, "snapshot", return_value=first) as snapshot:
        generate_forecasts(data_provider=provider, out_dir=tmp)
    snapshot.assert_called_once_with()

def test_decision_uses_explicit_runtime_threshold(self):
    decision = replace(default_runtime_config().decision, scale_out_threshold=0.7)
    advice = build_scaling_advice(item, future, decision_config=decision)
    self.assertEqual(advice["action"], "scale_out")
```

Add a provider test proving `step_seconds` and `rate_window` come from the supplied `CollectionConfig`, and an urgency test using an explicit `DecisionConfig` rather than a global singleton.

Extend the settings boundary test after consumer migration:

```python
def test_legacy_settings_singleton_and_runtime_dataclasses_are_removed(self):
    from resource_predict import settings as module
    self.assertFalse(hasattr(module, "settings"))
    self.assertFalse(hasattr(module, "GenerationConfig"))
    self.assertFalse(hasattr(module, "ForecastConfig"))
    self.assertFalse(hasattr(module, "DecisionConfig"))
    self.assertFalse(hasattr(module, "K8SPrometheusConfig"))
```

- [ ] **Step 2: Run focused pipeline/decision/provider tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_settings_boundary.py tests/test_forecast_windowing.py tests/test_decision.py tests/test_k8s_workload_decision.py tests/test_k8s_workload_provider.py tests/test_urgency.py`

Expected: failures because production functions do not accept runtime config sections.

- [ ] **Step 3: Thread the immutable snapshot through the pipeline**

At `generate_forecasts()` entry use exactly one snapshot:

```python
runtime = runtime_config or runtime_config_store.snapshot()
prediction_cfg = runtime.prediction
decision_cfg = runtime.decision
```

Put those frozen section objects in `WorkerContext`. Pass `decision_config` explicitly to VM and K8S advice builders and urgency computation. Replace deleted algorithm fields with named module constants at their existing decision/forecast calculation sites. Convert `PredictionConfig` into the existing model-options dictionary once per run; do not read JSON or the store per resource.

In K8S ingestion, capture one runtime snapshot at `run_k8s_prometheus_upsert()` entry. Use `collection.scheduled_update_interval_minutes + 60` for incremental history, `collection.history_days` for full history, and pass the same `CollectionConfig` to every target resolution/query so `step_seconds`, `rate_window`, and timeout remain consistent. Make `cluster_configs.read_cluster_config_payload()` read schedule hints from the runtime store during the transition. Make the legacy forecast service read/write the prediction section of runtime configuration until Task 5 removes that old API surface.

After every production consumer has moved, delete the old runtime dataclasses and `settings` singleton from `settings.py`; retain only `BootstrapSettings` and `bootstrap_settings` introduced in Task 2.

- [ ] **Step 4: Run focused pipeline and decision tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_settings_boundary.py tests/test_cluster_configs.py tests/test_forecast_config.py tests/test_forecast_windowing.py tests/test_forecasting.py tests/test_forecast_optimizations.py tests/test_pipeline_worker.py tests/test_decision.py tests/test_k8s_workload_decision.py tests/test_k8s_workload_provider.py tests/test_urgency.py`

Expected: all selected tests pass and no production module imports the removed `settings` singleton.

- [ ] **Step 5: Commit runtime snapshot consumption**

```bash
git add resource_predict tests/test_forecast_config.py tests/test_forecast_windowing.py tests/test_decision.py tests/test_k8s_workload_decision.py tests/test_k8s_workload_provider.py tests/test_urgency.py
git commit -m "refactor: use runtime config snapshots"
```

### Task 4: Reconfigure the single K8S scheduler thread at runtime

**Files:**
- Modify: `resource_predict/services/k8s_ingest.py`
- Modify: `app.py`
- Modify: `tests/test_scheduler_startup_delay.py`
- Modify: `tests/test_app_scheduler.py`
- Test: `tests/test_k8s_scheduler_reconfigure.py`

**Interfaces:**
- Consumes: `runtime_config_store.snapshot().collection`
- Produces: `notify_k8s_scheduler_config_changed() -> None`
- Preserves: `start_k8s_background_updater() -> threading.Thread` and `stop_k8s_background_updater(timeout=10.0) -> None`

- [ ] **Step 1: Add failing scheduler state-transition tests**

Use controllable fake events/config snapshots and implement these exact cases:

- `test_disabled_scheduler_waits_without_fetching`: start with `scheduled_update_enabled=False`, wait for a test-controlled reload boundary, and assert the patched fetch function has zero calls.
- `test_enable_notification_wakes_existing_thread_and_fetches`: change the store snapshot from disabled to enabled, call `notify_k8s_scheduler_config_changed()`, release the zero-delay startup gate, and assert the patched fetch function is called once.
- `test_interval_change_wakes_wait_and_uses_new_interval`: record timeout values passed to the scheduler wait helper, switch interval from 360 to 15 minutes, notify, and assert the next timeout is `900.0` seconds.
- `test_disable_during_fetch_finishes_current_fetch_then_waits`: block inside the patched fetch function, save a disabled snapshot and notify, release the fetch, and assert one completed call followed by no second call.
- `test_reconfigure_never_creates_a_second_thread`: call `start_k8s_background_updater()` twice and assert object identity, one alive thread named `k8s-updater`, and no implicit stop/restart.

Update the app lifecycle test so application startup always creates the one scheduler control thread in the serving process, even when scheduled pulling is disabled.

- [ ] **Step 2: Run scheduler tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_k8s_scheduler_reconfigure.py tests/test_scheduler_startup_delay.py tests/test_app_scheduler.py`

Expected: failures because the current loop has fixed interval arguments and no reload event.

- [ ] **Step 3: Implement event-driven single-thread reconfiguration**

Add `_k8s_reload_event`. The scheduler loop reads a fresh collection snapshot only at control boundaries. When disabled it waits for reload/stop without fetching. When enabled it applies the internal 60-second first-run delay, runs one fetch, then waits for either reload or the current configured interval. `notify_k8s_scheduler_config_changed()` only sets the reload event; it never calls `start_k8s_background_updater()`.

Use separate stop and reload events, clear reload only after consuming it, and preserve `fail_if_busy=False` for scheduled pulls.

- [ ] **Step 4: Run scheduler tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_k8s_scheduler_reconfigure.py tests/test_scheduler_startup_delay.py tests/test_app_scheduler.py tests/test_cluster_configs.py`

Expected: all scheduler and K8S update tests pass.

- [ ] **Step 5: Commit hot reconfiguration**

```bash
git add app.py resource_predict/services/k8s_ingest.py tests/test_k8s_scheduler_reconfigure.py tests/test_scheduler_startup_delay.py tests/test_app_scheduler.py
git commit -m "feat: hot reload k8s scheduler config"
```

### Task 5: Add transactional unified system configuration API

**Files:**
- Create: `resource_predict/services/system_config.py`
- Create: `resource_predict/api/system_config.py`
- Create: `tests/test_system_config.py`
- Modify: `resource_predict/services/cluster_configs.py`
- Modify: `app.py`
- Delete: `resource_predict/services/forecast_config.py`
- Delete: `resource_predict/api/forecast_config.py`
- Delete: `tests/test_forecast_config.py`

**Interfaces:**
- Produces: `read_system_config_payload() -> dict[str, Any]`
- Produces: `save_system_config_payload(payload: Any) -> dict[str, Any]`
- Produces: `GET /api/system-config` and `PUT /api/system-config`
- Consumes: runtime normalizer/store/writer, cluster normalizers, cluster paths, scheduler reload notification

- [ ] **Step 1: Write failing service and API transaction tests**

Test payload shape exactly:

```python
{
  "runtime": runtime_config_to_dict(default_runtime_config()),
  "vm_scaling_clusters": {},
  "k8s_prometheus_clusters": [],
  "supported_methods": [
    {"key": "arima", "label": "ARIMA"},
    {"key": "sarima", "label": "SARIMA"},
    {"key": "prophet", "label": "Prophet"},
    {"key": "seasonal_naive", "label": "Seasonal naive"},
    {"key": "rolling_mean", "label": "Rolling mean"},
  ],
  "warnings": [],
}
```

Assert a valid PUT writes all three files, swaps the store once, calls scheduler notification once, and returns normalized values. Assert an invalid cluster plus valid runtime writes nothing and retains the old snapshot. Patch the second/third replace to raise and assert originals are restored, the in-memory snapshot is unchanged, and no scheduler notification occurs. Assert validation responses contain `{"error": "step_seconds 必须为正整数", "field": "runtime.collection.step_seconds"}`.

- [ ] **Step 2: Run system-config tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_system_config.py`

Expected: import failure because the system-config service/API do not exist.

- [ ] **Step 3: Implement staged validation, recoverable multi-file save, and routes**

Normalize every payload section before writing. Serialize the three normalized payloads to sibling temporary files. Preserve old bytes/existence flags, replace targets, and on any exception restore prior bytes or remove only a target newly created by this attempted transaction. Swap `runtime_config_store` only after all target files are durable; then notify the scheduler.

Register only the unified read/save routes in `app.py`. Preserve `/api/cluster-configs/k8s-diagnose` and `/api/cluster-configs/k8s-fetch`. Remove the old forecast service/routes/tests rather than leaving compatibility shims.

- [ ] **Step 4: Run system, cluster, and API tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_system_config.py tests/test_cluster_configs.py tests/test_resource_api.py`

Expected: all selected tests pass and `rg "api/forecast-config|services.forecast_config" app.py resource_predict tests` has no matches.

- [ ] **Step 5: Commit the unified API**

```bash
git add app.py resource_predict/api resource_predict/services tests/test_system_config.py tests/test_cluster_configs.py
git commit -m "feat: add unified system configuration api"
```

### Task 6: Replace the configuration page with the unified operational form

**Required skill before implementation:** Invoke `frontend-design` and apply it within the approved existing visual system; do not redesign unrelated resource/detail views.

**Files:**
- Create: `static/js/system-config.js`
- Create: `tests/js/test_system_config.mjs`
- Modify: `templates/index.html`
- Modify: `static/js/app-state.js`
- Modify: `static/js/index.js`
- Modify: `static/css/index.css`

**Interfaces:**
- Consumes: `GET/PUT /api/system-config`
- Produces: `window.ResourceSystemConfig`
- Produces pure helpers: `parseNamespaceList(text)`, `ratioFromPercent(value)`, `runtimePayloadFromValues(values)`, and `fieldNameFromErrorPath(path)`
- Preserves existing K8S diagnose/fetch action APIs and cluster row add/remove behaviors

- [ ] **Step 1: Write failing pure frontend configuration tests**

```javascript
test("runtime payload normalizes percentages and namespaces", () => {
  const payload = runtimePayloadFromValues({
    scale_out_threshold_percent: "80",
    conservative_namespaces: "prod, production, prod",
    enabled_methods: ["prophet", "seasonal_naive"],
  });
  assert.equal(payload.decision.scale_out_threshold, 0.8);
  assert.deepEqual(payload.decision.conservative_namespaces, ["prod", "production"]);
});

test("server field path maps to one form control", () => {
  assert.equal(fieldNameFromErrorPath("runtime.collection.step_seconds"), "collection.step_seconds");
});
```

Also assert duration fields and booleans survive collection without lossy coercion.

- [ ] **Step 2: Run the frontend test and verify failure**

Run: `node --test tests/js/test_system_config.mjs`

Expected: failure because `static/js/system-config.js` does not exist.

- [ ] **Step 3: Implement the four-section system configuration workspace**

Move configuration-specific rendering/collection/save functions out of `index.js` into `system-config.js`. Render these direct sections: 数据采集, 预测配置, 扩缩容策略, 集群接入. Use selects for policy tier and Prophet routing-independent model selection, checkboxes for booleans/models, numeric inputs with explicit minute/second/percent suffix labels, and comma-separated namespace inputs.

Change navigation/title copy from 集群配置 to 系统配置. Keep one save button. During save, disable it and mark the workspace busy. On success rerender the normalized server payload. On error, keep DOM values, show summary status, add an error class/message to the control mapped from `field`, and focus that control.

Update the stale schedule hint to reflect enabled/disabled state, interval, incremental window (`interval + 60` minutes), and full-history days.

- [ ] **Step 4: Run frontend tests and browser smoke test**

Run: `node --test tests/js/test_system_config.mjs tests/js/test_charts.mjs tests/js/test_resource_list.mjs`

Then start the app with `python app.py`, open the system configuration view, and verify desktop plus narrow viewport rendering, load, invalid-field display, successful save, cluster add/remove, K8S diagnose, and manual fetch. Do not submit production-changing scaling actions.

Expected: all Node tests pass and no fields overflow or become unreachable at narrow width.

- [ ] **Step 5: Commit the page migration**

```bash
git add templates/index.html static/js/app-state.js static/js/index.js static/js/system-config.js static/css/index.css tests/js/test_system_config.mjs
git commit -m "feat: manage runtime config from system page"
```

### Task 7: Migrate repository defaults and documentation, then run full regression

**Files:**
- Create: `deploy/runtime_config.json`
- Delete: `deploy/forecast_config.json`
- Modify: `README.md`
- Modify: `docs/configuration.md`
- Modify: `docs/architecture.md`
- Modify: `docs/api-reference.md`
- Modify: `docs/development.md`

**Interfaces:**
- Consumes: the runtime JSON schema and unified API from Tasks 1 and 5
- Produces: concise operator documentation and a tracked default runtime config

- [ ] **Step 1: Add the canonical default runtime file and remove the superseded forecast file**

Create `deploy/runtime_config.json` with exactly the approved three sections and defaults. Remove tracked `deploy/forecast_config.json`; keep runtime loader fallback support for existing deployments only when the new file is absent.

- [ ] **Step 2: Update all documentation references and examples**

README remains concise and links to detailed docs. Document the unified 页面配置 workflow, bootstrap-only `settings.py`, 22-field runtime whitelist, immediate snapshot semantics, scheduler reconfiguration, unified API payload/errors, legacy forecast migration, and the fact that cluster credentials stay in their existing files. Remove claims that frozen dataclass settings are the user configuration surface.

- [ ] **Step 3: Run stale-reference and repository checks**

Run:

```bash
rg -n "forecast_config.json|/api/forecast-config|settings\.forecast|settings\.decision|settings\.generation|settings\.k8s_prometheus" README.md docs app.py resource_predict tests static templates deploy
.\.venv\Scripts\python.exe -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\vulture.exe app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/js/test_system_config.mjs tests/js/test_charts.mjs tests/js/test_resource_list.mjs
```

Expected: stale-reference search returns only the intentional legacy migration constant/test/documentation paragraph; every validation command exits successfully.

- [ ] **Step 4: Remove generated caches and inspect the final diff**

Remove only repository `__pycache__` directories outside `.venv`. Run `git diff --check`, verify no credentials or generated outputs are staged, and confirm unrelated `.codex_tmp/` and `.qoder/` content remains untouched.

- [ ] **Step 5: Commit migration and documentation**

```bash
git add deploy/runtime_config.json deploy/forecast_config.json README.md docs resource_predict static templates tests app.py check_outputs.py generate_forecasts.py
git commit -m "docs: migrate to unified runtime configuration"
```
