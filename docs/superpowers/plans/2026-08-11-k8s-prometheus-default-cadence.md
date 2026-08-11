# K8S Prometheus Default Cadence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change the effective K8S Prometheus defaults to a 600-second query step and a 15-minute CPU `rate()` window everywhere users or runtime fallbacks can observe them.

**Architecture:** Keep the existing configuration flow and PromQL construction unchanged. Update the authoritative runtime dataclass defaults, legacy/internal fallbacks, committed runtime configuration, frontend fallbacks, tests, and formal configuration documentation so every active source agrees on `step_seconds=600` and `rate_window="15m"`.

**Tech Stack:** Python dataclasses, Flask runtime configuration, JavaScript configuration UI, JSON deployment configuration, pytest/unittest, Markdown.

## Global Constraints

- Preserve explicit per-cluster `rate_window` values; only missing values use `15m`.
- Do not change Prometheus metric names, output schemas, Workload aggregation, interpolation, prediction, or scaling logic.
- Keep historical files under `docs/superpowers/specs/` and existing `docs/superpowers/plans/` unchanged.
- Use `apply_patch` for manual UTF-8 text edits.
- Run Python commands through `.\.venv\Scripts\python.exe`.
- Remove project `__pycache__` directories outside `.venv` after validation.

---

### Task 1: Align executable defaults and deployment configuration

**Files:**
- Modify: `tests/test_runtime_config.py:18-23`
- Modify: `resource_predict/services/runtime_config.py:28-39`
- Modify: `resource_predict/internal_settings.py:192-204`
- Modify: `resource_predict/providers/k8s_prometheus.py:133-143,788-799`
- Modify: `resource_predict/services/k8s_ingest.py:121-124`
- Modify: `static/js/index.js:403-410,489-503`
- Modify: `deploy/runtime_config.json:1-10`

**Interfaces:**
- Consumes: `CollectionConfig`, `K8SPrometheusConfig`, `PrometheusTarget`, and the existing runtime/cluster configuration payloads.
- Produces: Effective missing-value defaults of `step_seconds=600` and `rate_window="15m"`; explicit configured values remain unchanged.

- [ ] **Step 1: Change the runtime default test first**

Update `test_defaults_expose_only_runtime_whitelist` with both assertions:

```python
self.assertEqual(payload["collection"]["step_seconds"], 600)
self.assertEqual(payload["collection"]["rate_window"], "15m")
```

- [ ] **Step 2: Run the focused test and verify the old defaults fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_config.py::RuntimeConfigTest::test_defaults_expose_only_runtime_whitelist -q
```

Expected: FAIL because the actual defaults are still `300` and `"5m"`.

- [ ] **Step 3: Update Python defaults and fallback literals**

Apply these exact default changes:

```python
# resource_predict/services/runtime_config.py
step_seconds: int = 600
rate_window: str = "15m"
```

```python
# resource_predict/internal_settings.py
# Prometheus range query 步长，单位秒；600 表示 10 分钟一个点。
step_seconds: int = 600
# rate() 计算窗口，例如 "15m"、"30m"；建议覆盖至少 2~4 个 Prometheus 抓取样本。
rate_window: str = "15m"
```

```python
# resource_predict/providers/k8s_prometheus.py
rate_window: str = "15m"
```

Change the target-resolution fallback from:

```python
cfg.rate_window or "5m"
```

to:

```python
cfg.rate_window or "15m"
```

Change the ingest fallback from:

```python
getattr(settings.k8s_prometheus, "step_seconds", 300)
```

to:

```python
getattr(settings.k8s_prometheus, "step_seconds", 600)
```

- [ ] **Step 4: Update the committed deployment configuration and UI fallbacks**

Set the `collection` values in `deploy/runtime_config.json` to:

```json
"step_seconds": 600,
"rate_window": "15m"
```

Change the global collection form fallbacks in `static/js/index.js` to:

```javascript
${configInput("采样步长（秒）", "step_seconds", collection.step_seconds || 600, { type: "number" })}
${configInput("CPU Rate 窗口", "rate_window", collection.rate_window || "15m", { placeholder: "15m" })}
```

Change the per-cluster `rate_window` placeholder to `"15m"` while preserving `cfg.rate_window || ""` so absence still delegates to the global value.

- [ ] **Step 5: Run runtime and provider tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_config.py tests/test_k8s_workload_provider.py -q
```

Expected: PASS. Tests that explicitly use `step_seconds=300` or `rate_window="5m"` continue to pass because explicit values are intentionally supported.

- [ ] **Step 6: Commit the executable default changes**

```powershell
git add -- tests/test_runtime_config.py resource_predict/services/runtime_config.py resource_predict/internal_settings.py resource_predict/providers/k8s_prometheus.py resource_predict/services/k8s_ingest.py static/js/index.js deploy/runtime_config.json
git commit -m "config: adjust Prometheus cadence defaults"
```

---

### Task 2: Synchronize formal documentation and verify the repository

**Files:**
- Modify: `docs/configuration.md:78-90,133-180,184-192`
- Reference: `docs/superpowers/specs/2026-08-11-k8s-prometheus-default-cadence-design.md`

**Interfaces:**
- Consumes: The effective defaults established in Task 1.
- Produces: User-facing documentation that consistently describes `600` seconds, `15m`, and 144 samples in a 24-hour K8S test window.

- [ ] **Step 1: Update the configuration examples and default table**

Make these documentation substitutions in `docs/configuration.md`:

```json
"step_seconds": 600,
"rate_window": "15m"
```

Update the per-cluster example to `"rate_window": "15m"`. Update the `K8SPrometheusConfig` table row so the defaults contain `600` / `15m` instead of `300` / `5m`.

- [ ] **Step 2: Update the prediction-window conversion example**

Replace the example with:

```text
step_seconds=600 + workload_test_duration="24h" = 144 个测试点
```

- [ ] **Step 3: Scan active sources for stale default literals**

Run:

```powershell
rg -n --glob '!docs/superpowers/specs/**' --glob '!docs/superpowers/plans/**' --glob '!outputs/**' --glob '!static/vendor/**' --glob '!.venv/**' 'step_seconds[^\n]*300|rate_window[^\n]*5m|collection\.step_seconds \|\| 300|collection\.rate_window \|\| "5m"|placeholder: "5m"' resource_predict static deploy docs tests
```

Expected: no active default or documentation matches. Explicit test fixtures may still contain `step_seconds=300` or `rate_window="5m"`; inspect any result and retain it only when the test is intentionally verifying an explicit value rather than a default.

- [ ] **Step 4: Run compilation and relevant regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py generate_forecasts.py ingest_k8s_workloads.py check_outputs.py resource_predict tests
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_config.py tests/test_k8s_workload_provider.py -q
```

Expected: both commands exit with code 0.

- [ ] **Step 5: Remove generated Python caches outside `.venv`**

Resolve each `__pycache__` directory under the repository, exclude paths under `.venv`, verify every resolved path remains beneath the repository root, and remove only those verified cache directories with native PowerShell `Remove-Item -LiteralPath ... -Recurse -Force`.

- [ ] **Step 6: Check the final diff and commit documentation**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; unrelated `.codex_tmp/` and `.qoder/` remain untouched.

Commit only the formal documentation and this implementation plan:

```powershell
git add -- docs/configuration.md docs/superpowers/plans/2026-08-11-k8s-prometheus-default-cadence.md
git commit -m "docs: update Prometheus cadence defaults"
```
