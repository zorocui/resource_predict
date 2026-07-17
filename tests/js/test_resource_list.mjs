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
