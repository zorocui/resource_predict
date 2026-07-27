# Future Forecast Area Valid Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the orange forecast area cover only the valid future forecast interval and make forecast lines use the same strict missing-value rules.

**Architecture:** Keep the change inside the existing ECharts adapter in `static/js/charts.js`. A shared strict value predicate will feed both point construction and a pure `futureForecastRange(xPredFuture, predsFuture)` helper; `buildChartOption()` will build `markArea` only from that future-only range, never from test timestamps or test predictions.

**Tech Stack:** Browser JavaScript, Apache ECharts option objects, Node.js built-in `node:test`, Flask application for browser verification.

## Global Constraints

- The orange area uses only `x_pred_ms + preds_future`; `x_test_ms + preds` must never affect its boundaries.
- Treat `null`, `undefined`, blank strings, `NaN`, `Infinity`, and `-Infinity` as missing.
- Preserve numeric `0` and numeric string `"0"` as valid points.
- Use the union of valid future timestamps across all forecast models.
- Keep the area continuous across internal gaps, but hide it when fewer than two distinct valid future timestamps exist.
- Do not change prediction algorithms, output schemas, threshold lines, or data zoom behavior.
- Use UTF-8 and `apply_patch` for manual edits.
- Run project Python commands through `.\.venv\Scripts\python.exe`.

---

## File Structure

- Create `tests/js/test_charts.mjs`: isolated Node tests for strict point construction, future-only range calculation, and `markArea` integration.
- Modify `static/js/charts.js`: add the shared validity predicate, calculate future forecast boundaries from valid `preds_future` pairs, integrate the result into `markArea`, and export testable pure helpers.
- Modify `docs/architecture.md`: document that the orange area is future-only and follows valid visible forecast points.

### Task 1: Strict Point Validity and Future-Only Area Boundaries

**Files:**
- Create: `tests/js/test_charts.mjs`
- Modify: `static/js/charts.js:20-35`
- Modify: `static/js/charts.js:381-442`
- Modify: `static/js/charts.js:1130-1137`

**Interfaces:**
- Consumes: `xPredFuture: unknown[]` and `predsFuture: Record<string, unknown[]>`.
- Produces: `futureForecastRange(xPredFuture, predsFuture): {startMs: number, endMs: number} | null`.
- Produces: `toPairs(xMs, y): Array<[number, number]>` with strict missing-value filtering.
- Exposes both helpers and `buildChartOption` through `window.ResourceCharts` for Node regression tests.

- [ ] **Step 1: Create the failing chart tests**

Create `tests/js/test_charts.mjs` with this complete harness and test set:

```js
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

globalThis.window = {
  ResourcePredictApp: {
    chartAuxiliaryVisible: true,
    chartModeKey: "raw",
    chartRangeKey: "all",
    colorMap: { rolling_mean: "#7c3aed" },
    labelMap: { rolling_mean: "Rolling Mean", arima: "ARIMA" },
    metricTitleMap: { cpu: "CPU" },
    state: { selectedResourceId: "", selectedMetricKey: "" },
    els: {},
    selectedContainerByResource: new Map(),
    resourcePayloadCache: new Map(),
    chartDataByKey: new Map(),
    loadedChartKeys: new Set(),
  },
  ResourceApi: {},
  ResourceList: {
    isK8s: () => false,
    metricTitleFor: (_resource, metricKey) => metricKey.toUpperCase(),
    formatStatValue: (value) => String(value),
    formatMemoryGiB: (value) => `${value} GiB`,
  },
  addEventListener() {},
};

const source = fs.readFileSync("static/js/charts.js", "utf8");
vm.runInThisContext(source, { filename: "static/js/charts.js" });

const {
  buildChartOption,
  futureForecastRange,
  toPairs,
} = window.ResourceCharts;

const T0 = 1_800_000_000_000;
const HOUR = 60 * 60 * 1000;
const times = Array.from({ length: 9 }, (_, index) => T0 + index * HOUR);

test("toPairs rejects missing values without rejecting real zero", () => {
  const pairs = toPairs(
    times,
    [null, undefined, "", "   ", Number.NaN, Infinity, -Infinity, 0, "0"]
  );

  assert.deepEqual(pairs, [
    [times[7], 0],
    [times[8], 0],
  ]);
});

test("future range uses the union of valid future points across models", () => {
  const result = futureForecastRange(
    times.slice(0, 5),
    {
      rolling_mean: [null, 0.2, 0.3, null, null],
      arima: [null, null, 0.4, 0.5, null],
    }
  );

  assert.deepEqual(result, { startMs: times[1], endMs: times[3] });
});

test("internal missing points do not split or shrink valid outer bounds", () => {
  const result = futureForecastRange(
    times.slice(0, 5),
    { rolling_mean: [0.1, null, "", 0.4, 0.5] }
  );

  assert.deepEqual(result, { startMs: times[0], endMs: times[4] });
});

test("future range ignores models without arrays and mismatched array tails", () => {
  const result = futureForecastRange(
    times.slice(0, 4),
    {
      rolling_mean: [null, 0.2, 0.3],
      invalid: null,
    }
  );

  assert.deepEqual(result, { startMs: times[1], endMs: times[2] });
});

test("future range is absent with fewer than two distinct valid timestamps", () => {
  assert.equal(
    futureForecastRange(times.slice(0, 3), { rolling_mean: [null, 0.2, null] }),
    null
  );
  assert.equal(
    futureForecastRange(times.slice(0, 3), { rolling_mean: [null, "", undefined] }),
    null
  );
});

test("markArea starts at valid future data and never covers test predictions", () => {
  const testStart = T0;
  const futureStart = T0 + 3 * HOUR;
  const chartData = {
    x_train_ms: [testStart - HOUR],
    y_train: [0.2],
    x_test_ms: [testStart, testStart + HOUR],
    y_test: [0.3, 0.4],
    preds: { rolling_mean: [0.31, 0.41] },
    x_pred_ms: [futureStart, futureStart + HOUR, futureStart + 2 * HOUR],
    preds_future: { rolling_mean: [null, 0.5, 0.6] },
    metrics: { rolling_mean: { rmse: 0.01 } },
    best_method: "rolling_mean",
  };

  const option = buildChartOption(
    chartData,
    "cpu",
    "percent",
    { resource_type: "openstack_vm" }
  );
  const auxiliary = option.series.find((series) => series.markArea);

  assert.ok(auxiliary);
  assert.deepEqual(auxiliary.markArea.data, [[
    { xAxis: futureStart + HOUR },
    { xAxis: futureStart + 2 * HOUR },
  ]]);
  assert.ok(auxiliary.markArea.data[0][0].xAxis > chartData.x_test_ms.at(-1));
});
```

- [ ] **Step 2: Run the new test to verify it fails**

Run:

```powershell
node --test tests/js/test_charts.mjs
```

Expected: FAIL because `window.ResourceCharts.futureForecastRange`, `toPairs`, and `buildChartOption` are not exported, and the future-only boundary behavior is not implemented.

- [ ] **Step 3: Add the shared strict validity rule and pure range helper**

In `static/js/charts.js`, replace the current `toPairs()` implementation and add `futureForecastRange()` immediately after it:

```js
  function isValidChartValue(value) {
    if (value === null || value === undefined) return false;
    if (typeof value === "string" && !value.trim()) return false;
    return Number.isFinite(Number(value));
  }

  function toPairs(xMs, y) {
    const res = [];
    for (let i = 0; i < xMs.length; i++) {
      let ts = xMs[i];
      if (typeof ts === "number" && ts < 1e12) ts *= 1000;
      if (!Number.isFinite(ts) || !isValidChartValue(y[i])) continue;
      res.push([ts, Number(y[i])]);
    }
    return res;
  }

  function futureForecastRange(xPredFuture, predsFuture) {
    if (!Array.isArray(xPredFuture) || !predsFuture || typeof predsFuture !== "object") {
      return null;
    }
    let startMs = Infinity;
    let endMs = -Infinity;
    Object.values(predsFuture).forEach((values) => {
      if (!Array.isArray(values)) return;
      const pairCount = Math.min(xPredFuture.length, values.length);
      for (let index = 0; index < pairCount; index++) {
        if (!isValidChartValue(values[index])) continue;
        const ts = normalizeTsMs(xPredFuture[index]);
        if (!Number.isFinite(ts)) continue;
        startMs = Math.min(startMs, ts);
        endMs = Math.max(endMs, ts);
      }
    });
    return startMs < endMs ? { startMs, endMs } : null;
  }
```

This deliberately preserves numeric strings, including `"0"`, and rejects blank strings before `Number()` can convert them to zero.

- [ ] **Step 4: Build `markArea` from future predictions only**

In `buildChartOption()`, replace the raw `xPredFuture` first/last boundary calculation with:

```js
    if (app.chartAuxiliaryVisible) {
      const futureRange = futureForecastRange(xPredFuture, chartData.preds_future);
      const markLineData = auxiliaryMarkLines(metricKey, isPercentMode);
      const markArea = futureRange ? {
        silent: true,
        itemStyle: { color: "rgba(217,119,6,.09)" },
        data: [[
          { xAxis: futureRange.startMs },
          { xAxis: futureRange.endMs },
        ]],
      } : undefined;
```

Do not pass `x_test_ms`, `preds`, combined model-series data, or the history/test bridge point into `futureForecastRange()`.

- [ ] **Step 5: Export the pure helpers for regression tests**

Extend the existing `window.ResourceCharts` export:

```js
  window.ResourceCharts = {
    buildChartOption,
    closeChartModal,
    disposeDetailChart,
    futureForecastRange,
    openChartModal,
    renderDetail,
    toPairs,
    toggleChartAuxiliary,
  };
```

- [ ] **Step 6: Run the focused JavaScript tests**

Run:

```powershell
node --test tests/js/test_charts.mjs tests/js/test_resource_list.mjs
```

Expected: all chart and risk-list JavaScript tests PASS.

- [ ] **Step 7: Review the implementation diff and commit**

Run:

```powershell
git diff --check
git diff -- static/js/charts.js tests/js/test_charts.mjs
git add -- static/js/charts.js tests/js/test_charts.mjs
git commit -m "fix: align forecast area with valid future points"
```

Expected: `git diff --check` exits with code 0 and the commit includes only the chart implementation and its JavaScript tests.

### Task 2: Document and Verify the Forecast Area Contract

**Files:**
- Modify: `docs/architecture.md:300-312`

**Interfaces:**
- Consumes: `futureForecastRange()` behavior from Task 1.
- Produces: an architecture contract explaining the future-only orange area and strict missing-point semantics.

- [ ] **Step 1: Add the chart-area contract to architecture documentation**

Add this bullet after the existing risk-queue/detail-chart scope description in `docs/architecture.md`:

```markdown
- **未来预测辅助区**：详情图橙色区域只覆盖 `x_pred_ms + preds_future` 中全部可见模型的有效未来预测时间并集，不覆盖 `x_test_ms + preds` 测试/回测阶段。预测线与色带共用有效点规则：`null`、空字符串和非有限数均视为缺失，真实数值 `0` 保留；首尾缺失会收缩色带边界，中间缺失不会把未来区域拆段，有效未来时间不足两个时不显示色带。
```

- [ ] **Step 2: Run static and automated regression checks**

Run:

```powershell
node --test tests/js/test_charts.mjs tests/js/test_resource_list.mjs
.\.venv\Scripts\python.exe -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\vulture.exe app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
```

Expected:

- all Node tests PASS;
- compileall exits with code 0;
- pyflakes exits with code 0 and no findings;
- vulture exits with code 0 and no findings;
- the complete pytest suite passes, allowing only already-known warnings.

- [ ] **Step 3: Remove generated Python bytecode caches**

First enumerate and validate the exact targets:

```powershell
$repoRoot = (Resolve-Path '.').Path
$cacheDirs = Get-ChildItem -Path $repoRoot -Directory -Recurse -Filter '__pycache__' |
  Where-Object { $_.FullName -notlike \"$repoRoot\\.venv\\*\" }
$cacheDirs | ForEach-Object {
  if (-not $_.FullName.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw \"Refusing to remove path outside repository: $($_.FullName)\"
  }
  $_.FullName
}
```

After confirming every printed path is inside the repository and outside `.venv`, remove only those validated directories:

```powershell
$cacheDirs | Remove-Item -Recurse -Force
```

Expected: project `__pycache__` directories outside `.venv` are removed; source files and `.venv` are untouched.

- [ ] **Step 4: Verify the behavior in the real browser**

Start the local application:

```powershell
.\.venv\Scripts\python.exe app.py
```

Use the `browser:control-in-app-browser` skill to open the emitted local URL and inspect a resource detail chart with a controlled missing-future-point payload. Verify all of the following:

- the first and last orange-area timestamps equal the first and last valid future forecast timestamps;
- the orange area begins strictly after the last test timestamp;
- leading/trailing missing future values shrink the area;
- an internal missing future value does not split the area;
- `null` and blank values do not render as false zero points;
- a real zero value still renders;
- threshold lines and the bottom data-zoom control remain unchanged;
- browser console errors and warnings are empty.

Stop the local application after verification.

- [ ] **Step 5: Commit the documentation**

Run:

```powershell
git diff --check
git diff -- docs/architecture.md
git add -- docs/architecture.md
git commit -m "docs: explain future forecast area boundaries"
git status --short
```

Expected: documentation commit succeeds and `git status --short` is empty.
