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
const times = Array.from({ length: 9 }, (_, index) => T0 + index * HOUR);

test("calibrated upper bound preserves missing intervals and real zero", () => {
  const option = buildChartOption({x_train_ms:[times[0]],y_train:[0.1],x_test_ms:[times[1]],y_test:[0.2],
    test_end_ms:times[1],x_pred_ms:[times[2],times[3],times[4]],preds_future:{rolling_mean:[0.2,0.3,0]},
    calibration:{upper:[0.4,null,0]},preds:{},metrics:{},best_method:"rolling_mean"},"cpu","percent",{resource_type:"openstack_vm"});
  const upper = option.series.find(s=>s.name==="校准上界");
  assert.deepEqual(upper.data,[[times[2],0.4],[times[3],null],[times[4],0]]);
  assert.equal(upper.connectNulls,false);
  assert.ok(option.legend.data.includes("校准上界"));
  assert.match(option.tooltip.formatter([{value:[times[3],null],seriesName:"校准上界",marker:""}]),/校准上界: —/);
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
    { xAxis: testEnd },
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
    { xAxis: times[3] },
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
