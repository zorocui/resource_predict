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
