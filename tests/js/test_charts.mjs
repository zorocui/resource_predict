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
  futurePairsAfterTest,
  futureForecastRange,
  insertGapBreaks,
  lastValidTimestamp,
  toPairs,
} = window.ResourceCharts;

const T0 = 1_800_000_000_000;
const HOUR = 60 * 60 * 1000;

test("unavailable prediction displays history without claiming an old forecast", () => {
  const option = buildChartOption({x_train_ms:[T0], y_train:[.2],
    prediction_skipped:true, forecast_status:"unavailable", preds:{}, preds_future:{}}, "cpu");
  assert.match(option.title.subtext, /暂无预测/);
  assert.doesNotMatch(option.title.subtext, /沿用旧预测/);
  assert.ok(option.series.some(series => series.name === "历史"));
});

test("per-metric generation time is visible", () => {
  const option = buildChartOption({x_train_ms:[T0], y_train:[.2],
    forecast_generated_at_epoch_ms:T0, preds:{}, preds_future:{}}, "cpu");
  assert.match(option.title.subtext, /预测生成/);
});

test("stale chart states missing future explicitly and marks scaling without auxiliary lines", () => {
  const previous = window.ResourcePredictApp.chartAuxiliaryVisible;
  window.ResourcePredictApp.chartAuxiliaryVisible = false;
  try {
    const option = buildChartOption({best_method:"rolling_mean", x_train_ms:[T0], y_train:[.2],
      x_test_ms:[T0+HOUR], y_test:[.3], test_end_ms:T0+HOUR, prediction_skipped:true,
      preds:{rolling_mean:[.31]}, x_pred_ms:[T0+2*HOUR], preds_future:{rolling_mean:[.4]},
      latest_observation_ms:T0+3*HOUR,last_scaled_at_epoch_ms:T0+1.5*HOUR,
      x_observed_ms:[T0+3*HOUR], y_observed:[.42]},"cpu");
    assert.match(option.title.subtext,/沿用旧预测/);
    assert.ok(!option.legend.data.includes("旧预测测试"));
    assert.ok(!option.series.some(s=>s.name==="旧预测测试" || s.name==="测试"));
    assert.deepEqual(option.series.find(s=>s.name==="历史").data.filter(p=>p[1]!=null).map(p=>p[0]),[T0,T0+HOUR,T0+3*HOUR]);
    assert.ok(!option.series.some(s=>s.markArea));
    const marker = option.series.find(s=>s.name==="最近调配");
    assert.equal(marker.type,"scatter");
    assert.equal(marker.symbol,"circle");
    assert.deepEqual(marker.data,[[T0+HOUR,.3]]);
    assert.equal(marker.markLine,undefined);
    assert.match(marker.tooltip.formatter(),/最近调配.*定位至最近实际采样/);
  } finally { window.ResourcePredictApp.chartAuxiliaryVisible = previous; }
});

test("only the best model is initially visible and other legends remain selectable", () => {
  const option = buildChartOption({best_method:"arima", x_train_ms:[T0], y_train:[.2],
    x_test_ms:[T0+HOUR], y_test:[.3], test_end_ms:T0+HOUR,
    preds:{rolling_mean:[.25], arima:[.31]}, x_pred_ms:[T0+2*HOUR],
    preds_future:{rolling_mean:[.25], arima:[.4]}}, "cpu");
  assert.equal(option.legend.selected.ARIMA, true);
  assert.equal(option.legend.selected["Rolling Mean"], false);
  assert.equal(option.legend.selected["历史"], true);
  assert.equal(option.legend.selected["测试"], true);
  assert.equal(option.legend.selectedMode, "multiple");
  assert.ok(option.legend.data.includes("Rolling Mean"));
  assert.ok(option.series.some(series => series.name === "Rolling Mean" && series.data.length));
});

test("new observations join blue history and remove elapsed forecast shading", () => {
  const option = buildChartOption({best_method:"rolling_mean", x_train_ms:[T0], y_train:[.2],
    x_test_ms:[T0+HOUR], y_test:[.3], test_end_ms:T0+HOUR,
    preds:{rolling_mean:[.31]}, x_pred_ms:[T0+2*HOUR], preds_future:{rolling_mean:[.4]},
    x_observed_ms:[T0+2*HOUR,T0+3*HOUR], y_observed:[.42,.43], sample_interval_seconds:3600}, "cpu");
  const observed = option.series.find(s => s.name === "历史");
  assert.deepEqual(observed.data.filter(p => p[1] != null).map(p => p[0]), [T0,T0+HOUR,T0+2*HOUR,T0+3*HOUR]);
  assert.ok(!option.series.some(s => s.name === "旧预测测试"));
  assert.ok(!option.legend.data.includes("预测后实际观测"));
  assert.ok(!option.series.some(s => s.markArea));
});

test("future shading starts at the latest actual value while test predictions stay fixed", () => {
  const option = buildChartOption({best_method:"rolling_mean", x_train_ms:[T0], y_train:[.2],
    x_test_ms:[T0+HOUR], y_test:[.3], test_end_ms:T0+HOUR,
    preds:{rolling_mean:[.31]}, x_pred_ms:[T0+2*HOUR,T0+4*HOUR], preds_future:{rolling_mean:[.4,.5]},
    x_observed_ms:[T0+2*HOUR,T0+3*HOUR], y_observed:[.42,null], sample_interval_seconds:3600}, "cpu");
  const area = option.series.find(s => s.markArea).markArea.data[0];
  assert.equal(area[0].xAxis,T0+2*HOUR);
  assert.equal(area[0].name,"未来预测区");
  assert.equal(area[1].xAxis,T0+4*HOUR);
});
const times = Array.from({ length: 9 }, (_, index) => T0 + index * HOUR);

test("VM and Workload apply every time range and keep future predictions", () => {
  const app = window.ResourcePredictApp;
  const previousRange = app.chartRangeKey;
  const previousIsK8s = window.ResourceList.isK8s;
  window.ResourceList.isK8s = (resource) => resource.resource_type === "k8s_workload";
  const history = Array.from({ length: 10 * 24 }, (_, index) => T0 + index * HOUR);
  const boundary = T0 + 10 * 24 * HOUR;
  const data = {
    x_train_ms: history, y_train: history.map(() => 0.2),
    x_test_ms: [boundary], y_test: [0.3],
    preds: { rolling_mean: [0.31] },
    x_pred_ms: [boundary + HOUR, boundary + 2 * HOUR],
    preds_future: { rolling_mean: [0.32, 0.33] },
    best_method: "rolling_mean",
  };
  try {
    for (const [range, hours] of [["24h", 24], ["3d", 72], ["7d", 168], ["all", null]]) {
      app.chartRangeKey = range;
      const vmOption = buildChartOption(data, "cpu", "percent", { resource_type: "openstack_vm" });
      const workloadOption = buildChartOption(data, "cpu", "percent", { resource_type: "k8s_workload" });
      assert.equal(vmOption.xAxis.min, workloadOption.xAxis.min, range);
      assert.equal(vmOption.xAxis.max, workloadOption.xAxis.max, range);
      const visibleHistory = vmOption.series.find((series) => series.name === "历史").data;
      const expectedStart = hours === null ? T0 : boundary + HOUR - hours * HOUR;
      assert.equal(visibleHistory[0][0], expectedStart, range);
      assert.ok(vmOption.series.some((series) => series.data?.some((point) => point[0] === boundary + 2 * HOUR)), range);
    }
  } finally {
    app.chartRangeKey = previousRange;
    window.ResourceList.isK8s = previousIsK8s;
  }
});

test("detail advice renders backend scores, real zero, unknown and legacy without inferred formulas", () => {
  const app = {
    metricTitleMap: {}, viewMetricMap: { openstack_vm: [] },
    els: { detailConfidence: {}, detailAdvice: {}, detailActions: {} },
  };
  const context = vm.createContext({ window: {
    ResourcePredictApp: app, ResourceApi: {}, addEventListener() {},
    ScalingUI: { buildControls: () => "" },
  } });
  vm.runInContext(fs.readFileSync("static/js/resource-list.js", "utf8"), context);
  vm.runInContext(source, context);
  const resource = { resource_id: "vm-1", resource_type: "openstack_vm", scaling_advice: {
    action: "hold", confidence_score: 0, confidence_breakdown: {
      version: 2, score: 0, components: [{ label: "基础信号", value: 45 }, { label: "质量扣分", value: -45 }],
    },
  }, urgency_breakdown: { version: 2, score: 0, kind: "none" } };
  context.window.ResourceCharts.renderAdvice(resource);
  assert.match(app.els.detailConfidence.innerHTML, /置信度 低 · 0\/100/);
  assert.match(app.els.detailConfidence.innerHTML, /基础信号：45\n质量扣分：-45\n结果：45 − 45 ≈ 0 分/);
  assert.doesNotMatch(app.els.detailAdvice.innerHTML, /紧急度|无需调整 · 0\/100/);
  resource.scaling_advice.confidence_score = null;
  delete resource.scaling_advice.confidence_breakdown;
  delete resource.urgency_breakdown;
  resource.urgency_score = 182;
  context.window.ResourceCharts.renderAdvice(resource);
  assert.match(app.els.detailConfidence.innerHTML, /置信度 待评估/);
  assert.doesNotMatch(app.els.detailConfidence.innerHTML, /0\/100|默认中等置信度/);
  assert.doesNotMatch(app.els.detailAdvice.innerHTML, /紧急度|旧版排序分/);
  assert.doesNotMatch(app.els.detailAdvice.innerHTML, /182\/100/);
  assert.doesNotMatch(app.els.detailActions.innerHTML, /重新拉取预测/);
  app.viewMetricMap.k8s_workload = [];
  resource.resource_type = "k8s_workload";
  resource.resource_id = "k8s:c:ns:deployment:api";
  context.window.ResourceCharts.renderAdvice(resource);
  assert.match(app.els.detailActions.innerHTML, /data-workload-refresh="k8s:c:ns:deployment:api"/);
  assert.match(app.els.detailActions.innerHTML, /重新拉取预测/);
});

test("chart axis labels use Asia/Shanghai time", () => {
  const timestamp = Date.UTC(2026, 7, 4, 4, 0, 0);
  const option = buildChartOption(
    {
      x_train_ms: [timestamp],
      y_train: [0.2],
      x_test_ms: [timestamp + HOUR],
      y_test: [0.3],
      preds: { rolling_mean: [0.31] },
      x_pred_ms: [],
      preds_future: {},
      metrics: { rolling_mean: { rmse: 0.01 } },
      best_method: "rolling_mean",
    },
    "cpu",
    "percent",
    { resource_type: "openstack_vm" }
  );

  assert.equal(option.xAxis.axisLabel.formatter(timestamp), "08-04 12:00");
});

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

test("last valid timestamp normalizes seconds and ignores invalid values", () => {
  assert.equal(lastValidTimestamp([null, T0 / 1000, "", T0 + HOUR]), T0 + HOUR);
  assert.equal(lastValidTimestamp([null, "", Number.NaN]), null);
});

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

test("future pairs retain only valid points strictly after test end", () => {
  assert.deepEqual(
    futurePairsAfterTest(
      [times[2], times[3], times[4], times[5]],
      [0.1, 0.2, null, 0.4],
      times[3]
    ),
    [[times[5], 0.4]]
  );
});

test("markArea starts at test end and model future data excludes overlaps", () => {
  const testStart = T0;
  const testEnd = testStart + HOUR;
  const chartData = {
    x_train_ms: [testStart - HOUR],
    y_train: [0.2],
    x_test_ms: [testStart, testEnd],
    y_test: [0.3, 0.4],
    preds: { rolling_mean: [0.31, 0.41] },
    x_pred_ms: [testStart, testEnd, testEnd + HOUR, testEnd + 2 * HOUR],
    preds_future: { rolling_mean: [0.35, 0.45, 0.5, 0.6] },
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
    { name: "未来预测区", xAxis: testEnd },
    { xAxis: testEnd + 2 * HOUR },
  ]]);
  const model = option.series.find((series) => series.name === "Rolling Mean");
  const futurePoints = model.data.filter(([timestamp]) => timestamp > testEnd);
  assert.deepEqual(futurePoints, [
    [testEnd + HOUR, 0.5],
    [testEnd + 2 * HOUR, 0.6],
  ]);
  assert.equal(model.data.some(([timestamp, value]) => timestamp === testEnd && value === 0.45), false);
});

test("explicit test end metadata overrides an older x_test fallback", () => {
  const chartData = {
    x_train_ms: [times[0]],
    y_train: [0.1],
    x_test_ms: [times[1]],
    y_test: [0.2],
    test_end_ms: times[3],
    x_pred_ms: [times[2], times[4]],
    preds_future: { rolling_mean: [0.3, 0.4] },
    preds: { rolling_mean: [0.2] },
    metrics: { rolling_mean: { rmse: 0.01 } },
    best_method: "rolling_mean",
  };
  const option = buildChartOption(chartData, "cpu", "percent", { resource_type: "openstack_vm" });
  const auxiliary = option.series.find((series) => series.markArea);
  assert.deepEqual(auxiliary.markArea.data, [[
    { name: "未来预测区", xAxis: times[3] },
    { xAxis: times[4] },
  ]]);
});

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

test("chart applies emitted gap metadata to history and test series", () => {
  const chartData = {
    x_train_ms: [times[0], times[1], times[7]],
    y_train: [0.1, 0.2, 0.3],
    x_test_ms: [times[7], times[8]],
    y_test: [0.3, 0.4],
    x_pred_ms: [],
    preds_future: {},
    preds: {},
    metrics: {},
    sample_interval_seconds: 3600,
    max_interpolation_gap_steps: 3,
  };
  const option = buildChartOption(chartData, "cpu", "percent", { resource_type: "openstack_vm" });
  const history = option.series.find((series) => series.name !== "Rolling Mean" && series.z === 2);
  const testSeries = option.series.find((series) => series.z === 3);
  assert.deepEqual(history.data, [
    [times[0], 0.1],
    [times[1], 0.2],
    [times[1] + HOUR, null],
    [times[7] - HOUR, null],
    [times[7], 0.3],
  ]);
  assert.equal(history.connectNulls, false);
  assert.equal(testSeries.connectNulls, false);
});
