# K8S Workload Usage Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every K8S risk-queue usage chip explicitly identify its P95 as a Workload aggregate and explain the aggregation scope without expanding per-container details.

**Architecture:** Add pure presentation helpers to `resource-list.js` that derive aggregation wording and percentage-mode participation counts from the existing summary `spec`. Keep the API and forecast artifacts unchanged; render an inline information tooltip beside each K8S Workload P95, while the detail drawer continues to identify selected Container statistics.

**Tech Stack:** Browser JavaScript, Node.js built-in test runner, existing HTML/CSS tooltip component, Flask local UI, Python regression suite.

## Global Constraints

- Workload percentage is `sum(usage) / sum(Request or Limit)` at each timestamp followed by historical P95; never call it a container arithmetic average.
- Risk queue shows only Workload aggregate values; do not expand per-container details or load chart/detail endpoints for queue rows.
- Percentage mode displays `Workload 聚合 P95` and an exact participating/total count derived from positive container Request/Limit specs.
- Absolute mode displays `Workload 汇总 P95` and does not guess a per-metric container count unavailable in the summary.
- Preserve VM rendering, K8S action-based Limit/Request selection, output schemas, sorting, and detail-drawer Container statistics.
- Keep `README.md` Linux-facing and run project Python checks with `.\.venv\Scripts\python.exe`.
- Preserve unrelated worktree changes and remove project `__pycache__` directories outside `.venv` after checks.

---

## File Structure

- Create `tests/js/test_resource_list.mjs`: exercise the pure K8S presentation contract with Node's built-in test runner.
- Modify `static/js/resource-list.js`: calculate Workload scope text, tooltip copy, and integrate it into K8S metric chips.
- Modify `docs/architecture.md`: document queue Workload scope versus drawer Container scope.
- Modify `docs/configuration.md`: correct the summary-display description so it no longer claims both views always read the same scope.

### Task 1: Add Tested Workload Aggregation Presentation

**Files:**
- Create: `tests/js/test_resource_list.mjs`
- Modify: `static/js/resource-list.js`

**Interfaces:**
- Consumes: summary resource fields `resource_type`, `spec.containers`, `spec.containers_observed`, the four `spec` mode fields `cpu_limit_metric_mode`, `cpu_request_metric_mode`, `memory_limit_metric_mode`, `memory_request_metric_mode`, plus `observed_stats` and `scaling_advice.metric_actions`.
- Produces: `k8sWorkloadUsagePresentation(item, metricKey, displayUnit) -> { label: string, tooltip: string }` and unchanged risk-row HTML with explicit Workload aggregation text.

- [ ] **Step 1: Create failing JavaScript contract tests**

Create `tests/js/test_resource_list.mjs`:

```javascript
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

globalThis.window = {
  ResourcePredictApp: {
    metricTitleMap: {
      cpu: "CPU",
      memory: "内存",
      disk: "磁盘",
      cpu_limit: "CPU Limit",
      cpu_request: "CPU Request",
      memory_limit: "内存 Limit",
      memory_request: "内存 Request",
    },
    viewMetricMap: {
      openstack_vm: ["cpu", "memory", "disk"],
      k8s_workload: ["cpu_request", "cpu_limit", "memory_request", "memory_limit"],
    },
    state: { loadedItems: [], visibleItems: [], selectedResourceId: "" },
    els: {},
  },
};

const source = fs.readFileSync("static/js/resource-list.js", "utf8");
vm.runInThisContext(source, { filename: "static/js/resource-list.js" });

const list = window.ResourceList;

function k8sItem() {
  return {
    resource_type: "k8s_workload",
    spec: {
      containers_observed: ["app", "sidecar", "exporter"],
      containers: {
        app: { cpu_limit_cores: 1, memory_limit_gb: 1 },
        sidecar: { cpu_limit_cores: 0.5 },
        exporter: {},
      },
      cpu_limit_metric_mode: "cpu_usage/cpu_limit",
      memory_limit_metric_mode: "memory_working_set_gb",
    },
    observed_stats: {
      cpu_limit: { p95: 0.667 },
      memory_limit: { p95: 0.8 },
    },
    scaling_advice: {
      metric_actions: { cpu: "hold", memory: "hold" },
      target_spec: {},
    },
  };
}

test("percentage presentation identifies weighted Workload aggregation and participation", () => {
  const result = list.k8sWorkloadUsagePresentation(k8sItem(), "cpu_limit", "percent");
  assert.equal(result.label, "Workload 聚合 P95");
  assert.match(result.tooltip, /使用量总和 ÷ Limit 总和/);
  assert.match(result.tooltip, /不是容器使用率的算术平均/);
  assert.match(result.tooltip, /参与计算：2\/3 个容器/);
  assert.match(result.tooltip, /完整历史观测窗口/);
});

test("absolute presentation uses sum wording and does not guess a count", () => {
  const result = list.k8sWorkloadUsagePresentation(k8sItem(), "memory_limit", "gib");
  assert.equal(result.label, "Workload 汇总 P95");
  assert.match(result.tooltip, /有该指标数据的观测容器使用量之和/);
  assert.doesNotMatch(result.tooltip, /使用量总和 ÷/);
  assert.doesNotMatch(result.tooltip, /3\/3/);
});

test("missing container metadata is explicit", () => {
  const item = k8sItem();
  item.spec.containers = {};
  item.spec.containers_observed = [];
  const result = list.k8sWorkloadUsagePresentation(item, "cpu_limit", "percent");
  assert.match(result.tooltip, /参与容器：范围信息缺失/);
});

test("risk-row metric HTML contains the Workload aggregation label", () => {
  const html = list.metricSummary(k8sItem());
  assert.match(html, /CPU Limit/);
  assert.match(html, /Workload 聚合 P95 66\.7%/);
  assert.match(html, /参与计算：2\/3 个容器/);
});
```

- [ ] **Step 2: Run the JavaScript test and verify it fails**

```powershell
node --test tests/js/test_resource_list.mjs
```

Expected: FAIL because `k8sWorkloadUsagePresentation` and `metricSummary` are not exported.

- [ ] **Step 3: Implement the pure presentation helper**

Add after `baseMetricKey()` in `static/js/resource-list.js`:

```javascript
  const K8S_METRIC_SPEC_FIELDS = {
    cpu_limit: "cpu_limit_cores",
    cpu_request: "cpu_request_cores",
    memory_limit: "memory_limit_gb",
    memory_request: "memory_request_gb",
  };

  function k8sContainerNames(item) {
    const spec = item?.spec || {};
    const containers = spec.containers && typeof spec.containers === "object" && !Array.isArray(spec.containers)
      ? spec.containers
      : {};
    const names = new Set(Object.keys(containers).map((name) => String(name || "").trim()).filter(Boolean));
    if (Array.isArray(spec.containers_observed)) {
      spec.containers_observed.forEach((name) => {
        const value = String(name || "").trim();
        if (value) names.add(value);
      });
    }
    return { containers, names: Array.from(names) };
  }

  function k8sWorkloadUsagePresentation(item, metricKey, displayUnit) {
    const isPercent = displayUnit === "percent";
    const label = isPercent ? "Workload 聚合 P95" : "Workload 汇总 P95";
    const lines = [];
    if (isPercent) {
      const denominator = String(metricKey).endsWith("_request") ? "Request" : "Limit";
      lines.push(`统计口径：参与容器使用量总和 ÷ ${denominator} 总和，不是容器使用率的算术平均。`);
      const { containers, names } = k8sContainerNames(item);
      const field = K8S_METRIC_SPEC_FIELDS[metricKey];
      if (!field || !names.length) {
        lines.push("参与容器：范围信息缺失。");
      } else {
        const participating = names.filter((name) => {
          const value = Number(containers[name]?.[field]);
          return Number.isFinite(value) && value > 0;
        }).length;
        lines.push(`参与计算：${participating}/${names.length} 个容器。`);
      }
    } else {
      lines.push("统计口径：有该指标数据的观测容器使用量之和。");
      lines.push("参与范围：有该指标数据的观测容器。");
    }
    lines.push("统计范围：完整历史观测窗口的 P95。");
    return { label, tooltip: lines.join("\n") };
  }
```

- [ ] **Step 4: Integrate the helper into K8S metric chips**

Replace the P95 construction inside `k8sMetricSummary()` with:

```javascript
      const presentation = k8sWorkloadUsagePresentation(item, representative.key, unit);
      const p95 = stat.p95 !== undefined
        ? `${presentation.label} ${formatStatValue(stat.p95, unit)}`
        : actionLabel(action);
      const label = metricTitleFor(item, representative.key);
      const scopeInfo = stat.p95 !== undefined
        ? ` ${infoTooltip(presentation.tooltip, `${label} Workload 聚合口径`)}`
        : "";
      chips.push(`<span class="metric-pill is-${escapeHtml(action)}">${escapeHtml(label)} · ${escapeHtml(p95)}${scopeInfo}</span>`);
```

Export both testable functions in `window.ResourceList`:

```javascript
    k8sWorkloadUsagePresentation,
    metricSummary,
```

- [ ] **Step 5: Run the JavaScript tests and verify they pass**

```powershell
node --test tests/js/test_resource_list.mjs
```

Expected: 4 tests pass.

- [ ] **Step 6: Commit the tested frontend behavior**

```bash
git add static/js/resource-list.js tests/js/test_resource_list.mjs
git commit -m "feat: clarify k8s workload usage scope"
```

### Task 2: Document and Verify the UI Contract

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/configuration.md`

**Interfaces:**
- Consumes: `k8sWorkloadUsagePresentation()` and the unchanged detail-drawer Container labels.
- Produces: documented Workload/Container scope distinction and browser-verified UI behavior.

- [ ] **Step 1: Update architecture documentation**

Add to the K8S frontend description in `docs/architecture.md`:

```markdown
- **风险队列统计范围**：K8S 指标胶囊展示完整历史观测窗口的 Workload 聚合 P95。百分比按参与容器使用量总和除以对应 Request/Limit 总和计算，并显示参与容器数；这不是容器使用率的算术平均。详情抽屉的统计在容器图表加载后展示当前选中 Container 的范围，两者通过标签和提示明确区分。
```

- [ ] **Step 2: Correct configuration documentation**

Replace the sentence in `docs/configuration.md` claiming risk queue and detail drawer always read the same historical statistics with:

```markdown
每个资源包含轻量 `observed_stats`，按指标保存完整历史观测窗口的 `avg`、`p95`、`peak`。风险队列使用该字段展示资源级统计：VM 为 Resource，K8S 为 Workload 聚合；K8S 详情抽屉在容器图表加载后展示当前选中 Container 的统计，并明确标注范围。`history_coverage` 记录各指标历史覆盖时长，包含 `span_hours`、`span_days`、`threshold_days=5`、`is_short` 等字段；当历史不足 5 天且建议不是 `hold` 时，系统会将建议置信度降级到执行阈值以下，前端也会显示“历史不足 5 天”提示。
```

- [ ] **Step 3: Run focused JavaScript tests**

```powershell
node --test tests/js/test_resource_list.mjs
```

Expected: 4 tests pass.

- [ ] **Step 4: Start the local Flask application for browser verification**

```powershell
.\.venv\Scripts\python.exe app.py
```

Expected: Flask listens on the configured local host and port. Keep this command in its own terminal session for the next step.

- [ ] **Step 5: Verify a multi-container and single-container row in the browser**

Using the in-app browser, open the local application and verify:

1. A multi-container K8S row contains `Workload 聚合 P95` and its info tooltip shows the formula and an `N/M 个容器` count.
2. A single-container K8S row shows `1/1 个容器`.
3. Opening the multi-container row keeps the drawer label `P95 · Container` with the selected container name.
4. No container chart requests are made merely by rendering the risk queue.
5. VM rows retain their existing metric wording.

Expected: all five checks match the approved design; capture a screenshot of the multi-container row and open tooltip as verification evidence.

- [ ] **Step 6: Run complete automated regression checks**

Run separately:

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\vulture.exe app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/js/test_resource_list.mjs
```

Expected: every command exits 0; Python reports the full suite passing and Node reports 4 passing tests.

- [ ] **Step 7: Clean generated Python caches**

First list project caches outside `.venv` and verify every path is beneath `C:\Users\czh\Desktop\云资源使用预测\resource_predict`:

```powershell
Get-ChildItem -LiteralPath . -Directory -Recurse -Filter __pycache__ |
    Where-Object { $_.FullName -notlike "*\.venv\*" } |
    Select-Object -ExpandProperty FullName
```

Remove only the verified `__pycache__` directories with explicit PowerShell `Remove-Item -LiteralPath` commands; never recursively remove a computed or unverified path.

- [ ] **Step 8: Review and commit documentation**

```bash
git diff --check
git diff -- static/js/resource-list.js tests/js/test_resource_list.mjs docs/architecture.md docs/configuration.md
git status --short
git add docs/architecture.md docs/configuration.md
git commit -m "docs: explain workload usage aggregation"
```

Expected: only the approved frontend, tests, and documentation are present; the documentation commit succeeds and the worktree is clean.
