# Chart Timezone Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display chart axis labels and tooltips in `Asia/Shanghai` while preserving Prometheus epoch timestamps and chart point positions.

**Architecture:** Keep the fix inside the existing ECharts formatting adapter in `static/js/charts.js`. Verify the user-visible axis label through the existing JavaScript unit-test harness without exposing a new production API.

**Tech Stack:** Browser JavaScript, ECharts option builders, Node.js built-in test runner

## Global Constraints

- Do not modify Prometheus queries, provider timestamps, serialized epoch values, forecasting windows, or point positions.
- Use the IANA time zone name `Asia/Shanghai`.
- Preserve existing chart handling for missing values, zero values, forecast ranges, axes, and tooltips.
- Exclude generated and vendor paths from searches.

---

### Task 1: Render chart time in Asia/Shanghai

**Files:**
- Modify: `tests/js/test_charts.mjs`
- Modify: `static/js/charts.js:5`

**Interfaces:**
- Consumes: `buildChartOption(chartData, metricKey, displayUnit, resource)`
- Produces: ECharts axis and tooltip formatter output in `Asia/Shanghai`

- [ ] **Step 1: Add a failing timezone display test**

Add a test that builds a short-range chart around `Date.UTC(2026, 7, 4, 4, 0, 0)` and calls `option.xAxis.axisLabel.formatter(timestamp)`. Assert that the result is `"08-04 12:00"`.

- [ ] **Step 2: Run the JavaScript test and verify failure**

Run: `node --test tests/js/test_charts.mjs`

Expected: the new assertion fails with actual UTC text `"08-04 04:00"`.

- [ ] **Step 3: Apply the minimal timezone change**

Change the chart formatter constant from:

```javascript
const CHART_TIME_ZONE = "UTC";
```

to:

```javascript
const CHART_TIME_ZONE = "Asia/Shanghai";
```

- [ ] **Step 4: Run JavaScript and project regression checks**

Run:

```bash
node --test tests/js/test_charts.mjs
.\.venv\Scripts\python.exe -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\vulture.exe app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: every command exits successfully.

- [ ] **Step 5: Commit the tested fix**

```bash
git add static/js/charts.js tests/js/test_charts.mjs
git commit -m "fix: display charts in shanghai time"
```

