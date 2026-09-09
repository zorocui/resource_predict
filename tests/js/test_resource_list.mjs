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

test("confidence labels use actual threshold scores, including zero and absent values", () => {
  for (const [score, label] of [[0, "低"], [44.9, "低"], [45, "中"], [71.9, "中"], [72, "高"], [100, "高"]]) {
    assert.equal(list.confidenceText({ scaling_advice: { confidence_score: score, confidence: "high" } }), `${label} · ${score}/100`);
  }
  for (const score of [null, undefined, "", " ", NaN, Infinity, -Infinity, -1, 101]) {
    assert.equal(list.confidenceText({ scaling_advice: { confidence_score: score } }), "待评估");
  }
});

test("urgency distinguishes capacity, savings, hold, missing evidence and legacy scores", () => {
  for (const [score, label] of [[0, "低"], [39.9, "低"], [40, "中"], [69.9, "中"], [70, "高"], [89.9, "高"], [90, "紧急"], [100, "紧急"]]) {
    assert.equal(list.urgencyText({ urgency_breakdown: { version: 2, score, kind: "capacity_risk" } }), `容量风险 ${label} · ${score}/100`);
  }
  assert.equal(list.urgencyText({ urgency_breakdown: { version: 2, score: 95, kind: "savings" } }), "节省机会 极高 · 95/100");
  assert.equal(list.urgencyText({ urgency_breakdown: { version: 2, score: 0, kind: "none" } }), "无需调整 · 0/100");
  assert.equal(list.urgencyText({ urgency_breakdown: { version: 2, score: 0, kind: "unknown" } }), "待评估");
  for (const score of [null, undefined, "", " ", NaN, Infinity]) {
    assert.equal(list.urgencyText({ urgency_score: 0, urgency_breakdown: { version: 2, score, kind: "capacity_risk" } }), "待评估");
  }
  assert.equal(list.urgencyText({ urgency_score: 182 }), "旧版排序分 · 182");
  assert.equal(list.urgencyText({ urgency_score: 0 }), "旧版排序分 · 0");
  assert.doesNotMatch(list.urgencyTooltip({ urgency_score: 182 }), /\/100/);
});

test("tooltips use backend components without invented confidence bonuses or additive urgency metrics", () => {
  const item = { urgency_breakdown: { version: 2, score: 82, kind: "capacity_risk",
    components: [{ label: "容量压力", value: 82 }], metric_scores: [{ metric: "cpu", action: "scale_out", container: "app", value: 82 }] },
    scaling_advice: { confidence_score: 71, confidence_metric_scores: { cpu: 99 },
      target_k8s_policy: { ready_for_execution: true },
      confidence_breakdown: { version: 2, score: 71, components: [{ label: "同方向指标", value: 90 }, { label: "质量扣分", value: -19 }] } } };
  assert.match(list.confidenceTooltip(item), /同方向指标90 - 质量扣分19/);
  assert.match(list.confidenceTooltip(item), /不是预测正确的概率/);
  assert.doesNotMatch(list.confidenceTooltip(item), /执行就绪加成|最高指标得分|其他调整/);
  assert.match(list.urgencyTooltip(item), /容量压力82/);
  assert.match(list.urgencyTooltip(item), /app · CPU 扩容: 82/);
  assert.match(list.urgencyTooltip(item), /未经生产回放校准/);
  delete item.scaling_advice.confidence_breakdown;
  assert.match(list.confidenceTooltip(item), /缺少新版评分分解/);
  assert.doesNotMatch(list.confidenceTooltip(item), /同方向指标|99|默认中等置信度/);
});
