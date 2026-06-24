(function () {
  const app = window.ResourcePredictApp;
  const api = window.ResourceApi;
  const list = window.ResourceList;
  const CHART_TIME_ZONE = "UTC";
  const MINUTE_MS = 60 * 1000;
  const HOUR_MS = 60 * MINUTE_MS;
  const DAY_MS = 24 * HOUR_MS;
  const DEFAULT_HISTORY_POINTS = 1000;
  const MAX_HISTORY_POINTS = 10000;
  const MAX_RESOURCE_CACHE_ITEMS = 100;
  const MAX_CHART_CACHE_ITEMS = 400;
  const CHART_RANGES = [
    { key: "24h", label: "24h", durationMs: DAY_MS },
    { key: "3d", label: "3d", durationMs: 3 * DAY_MS },
    { key: "7d", label: "7d", durationMs: 7 * DAY_MS },
    { key: "all", label: "全部", durationMs: null },
  ];
  const CHART_MODES = [
    { key: "trend", label: "趋势" },
    { key: "peak", label: "峰值" },
    { key: "raw", label: "原始" },
  ];
  function toPairs(xMs, y) {
    const res = [];
    for (let i = 0; i < xMs.length; i++) {
      let ts = xMs[i];
      if (typeof ts === "number" && ts < 1e12) ts *= 1000;
      const val = Number(y[i]);
      if (Number.isFinite(ts) && Number.isFinite(val)) res.push([ts, val]);
    }
    return res;
  }

  function normalizeTsMs(t) {
    if (typeof t !== "number" || !Number.isFinite(t)) return NaN;
    return t < 1e12 ? t * 1000 : t;
  }

  function formatMs(ms, variant) {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: CHART_TIME_ZONE,
      month: "2-digit",
      day: "2-digit",
      hour: variant === "md" ? undefined : "2-digit",
      minute: variant === "mdhm" || variant === "mdh0" ? "2-digit" : undefined,
      second: variant === "full" ? "2-digit" : undefined,
      hour12: false,
    }).formatToParts(new Date(ms));
    const pick = (type) => parts.find((x) => x.type === type)?.value || "";
    const mm = pick("month");
    const dd = pick("day");
    if (variant === "md") return `${mm}-${dd}`;
    const hh = pick("hour") || "00";
    if (variant === "mdh0") return `${mm}-${dd} ${hh}:00`;
    if (variant === "full") return `${mm}-${dd} ${hh}:${pick("minute") || "00"}:${pick("second") || "00"}`;
    return `${mm}-${dd} ${hh}:${pick("minute") || "00"}`;
  }

  function chartRange() {
    return CHART_RANGES.find((item) => item.key === app.chartRangeKey)
      || CHART_RANGES.find((item) => item.key === "7d")
      || CHART_RANGES[0];
  }

  function chartMode() {
    return CHART_MODES.find((item) => item.key === app.chartModeKey) || CHART_MODES[0];
  }

  function collectTimes(groups) {
    const bucket = [];
    for (const group of groups) {
      if (!Array.isArray(group)) continue;
      for (const pair of group) {
        const ms = normalizeTsMs(Array.isArray(pair) ? pair[0] : pair);
        if (Number.isFinite(ms)) bucket.push(ms);
      }
    }
    bucket.sort((a, b) => a - b);
    return bucket;
  }

  function resolveChartWindow(xTrain, xTest, xPredFuture, isVm) {
    const allTimes = collectTimes([xTrain, xTest, xPredFuture]);
    if (!allTimes.length) return { min: undefined, max: undefined, spanMs: 0 };
    const fullMin = allTimes[0];
    const fullMax = allTimes[allTimes.length - 1];
    // VM 资源始终显示全部原始数据
    if (isVm) return { min: fullMin, max: fullMax, spanMs: fullMax - fullMin };
    const selectedRange = chartRange();
    if (!selectedRange.durationMs) {
      return { min: fullMin, max: fullMax, spanMs: fullMax - fullMin };
    }
    const futureTimes = collectTimes([xPredFuture]);
    const observedTimes = collectTimes([xTrain, xTest]);
    const anchor = futureTimes[0] || observedTimes[observedTimes.length - 1] || fullMax;
    const min = Math.max(fullMin, anchor - selectedRange.durationMs);
    return { min, max: fullMax, spanMs: fullMax - min };
  }

  function filterPairsByWindow(pairs, windowInfo) {
    if (!Array.isArray(pairs) || !pairs.length) return [];
    const min = Number.isFinite(windowInfo?.min) ? windowInfo.min : -Infinity;
    const max = Number.isFinite(windowInfo?.max) ? windowInfo.max : Infinity;
    return pairs.filter((pair) => pair[0] >= min && pair[0] <= max);
  }

  function chooseBucketMs(spanMs, pointCount, isVm) {
    // VM 资源直接显示原始数据点，不做聚合
    if (isVm || chartMode().key === "raw" || !Number.isFinite(spanMs) || spanMs <= 12 * HOUR_MS) return 0;
    if (spanMs <= 3 * DAY_MS) return 15 * MINUTE_MS;
    if (spanMs <= 14 * DAY_MS) return HOUR_MS;
    return pointCount > 2000 ? 6 * HOUR_MS : HOUR_MS;
  }

  function aggregatePairs(pairs, bucketMs) {
    if (!bucketMs || pairs.length < 3) return pairs;
    const modeKey = chartMode().key;
    const buckets = new Map();
    for (const [ts, value] of pairs) {
      const bucketTs = Math.floor(ts / bucketMs) * bucketMs;
      const current = buckets.get(bucketTs) || { sum: 0, count: 0, max: -Infinity };
      current.sum += value;
      current.count += 1;
      current.max = Math.max(current.max, value);
      buckets.set(bucketTs, current);
    }
    return Array.from(buckets.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([ts, stats]) => [ts, modeKey === "peak" ? stats.max : stats.sum / stats.count]);
  }

  function prepareSeriesData(pairs, windowInfo, isVm) {
    const visible = filterPairsByWindow(pairs, windowInfo);
    return aggregatePairs(visible, chooseBucketMs(windowInfo.spanMs, visible.length, isVm));
  }

  function prependBridgePoint(seriesData, bridgePoint) {
    if (!Array.isArray(seriesData) || !seriesData.length || !Array.isArray(bridgePoint)) {
      return seriesData;
    }
    const first = seriesData[0];
    if (!Array.isArray(first)) return seriesData;
    if (first[0] === bridgePoint[0]) return seriesData;
    return [bridgePoint].concat(seriesData);
  }

  function buildTimeAxisConfigFromPairs(groups, windowInfo) {
    const bucket = collectTimes(groups);
    if (!bucket.length) return { spanMs: 0 };
    const start = Number.isFinite(windowInfo?.min) ? windowInfo.min : bucket[0];
    const end = Number.isFinite(windowInfo?.max) ? windowInfo.max : bucket[bucket.length - 1];
    const spanMs = end - start;
    const pad = Math.max(spanMs * 0.02, 60 * 1000);
    return { min: start - pad, max: end + pad, spanMs };
  }

  function chartGridLeft(displayUnit) {
    return 12;
  }

  function chartYAxisLabelMargin(displayUnit) {
    return displayUnit === "gib" ? 12 : 8;
  }

  function metricThresholds(metricKey) {
    const title = app.metricTitleMap[metricKey] || metricKey;
    return {
      upper: { value: 0.8, label: `${title} 扩容阈值` },
      lower: { value: 0.2, label: `${title} 缩容阈值` },
    };
  }

  function auxiliaryMarkLines(metricKey, isPercentMode) {
    if (!isPercentMode) return [];
    const thresholds = metricThresholds(metricKey);
    return [
      { yAxis: thresholds.upper.value, name: thresholds.upper.label, lineStyle: { color: "rgba(220,38,38,.62)", type: "dashed", width: 1.2 } },
      { yAxis: thresholds.lower.value, name: thresholds.lower.label, lineStyle: { color: "rgba(5,150,105,.62)", type: "dashed", width: 1.2 } },
    ];
  }

  function metricContainerScope(resource, metricKey, displayUnit) {
    if (!resource || !list.isK8s(resource)) return "";
    const spec = resource.spec || {};
    const containers = spec.containers && typeof spec.containers === "object" && !Array.isArray(spec.containers)
      ? spec.containers
      : {};
    const observed = new Set();
    Object.keys(containers).forEach((name) => {
      if (String(name || "").trim()) observed.add(String(name).trim());
    });
    if (Array.isArray(spec.containers_observed)) {
      spec.containers_observed.forEach((name) => {
        const value = String(name || "").trim();
        if (value) observed.add(value);
      });
    }
    const allNames = Array.from(observed).sort();
    const scope = {
      cpu_limit: ["cpu_limit_cores", "CPU Limit"],
      cpu_request: ["cpu_request_cores", "CPU Request"],
      memory_limit: ["memory_limit_gb", "内存 Limit"],
      memory_request: ["memory_request_gb", "内存 Request"],
    }[metricKey];
    if (!scope) return "";
    if (displayUnit !== "percent") {
      return allNames.length ? `计算容器：${formatContainerNames(allNames)}（绝对值）` : "计算容器：全部观测容器（绝对值）";
    }
    const [field, label] = scope;
    const scopedNames = allNames.filter((name) => {
      const item = containers[name];
      return item && item[field] !== undefined && item[field] !== null && item[field] !== "";
    });
    if (!scopedNames.length) return `计算容器：有 ${label} 的容器`;
    return `计算容器：${formatContainerNames(scopedNames)}（有 ${label}）`;
  }

  function formatContainerNames(names) {
    const clean = names.map((name) => String(name || "").trim()).filter(Boolean);
    if (clean.length <= 3) return clean.join(", ");
    return `${clean.slice(0, 3).join(", ")} 等 ${clean.length} 个`;
  }

  function containerChartEntries(resource, metricKey) {
    const raw = resource?.container_charts;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    return Object.keys(raw).sort().map((name) => ({
      name,
      chart: raw[name]?.[metricKey],
    })).filter((item) => item.chart && typeof item.chart === "object");
  }

  function availableContainerNames(resource) {
    if (!list.isK8s(resource)) return [];
    const names = new Set();
    const spec = resource?.spec || {};
    const containers = spec.containers && typeof spec.containers === "object" && !Array.isArray(spec.containers)
      ? spec.containers
      : {};
    Object.keys(containers).forEach((name) => {
      const value = String(name || "").trim();
      if (value) names.add(value);
    });
    if (Array.isArray(spec.containers_observed)) {
      spec.containers_observed.forEach((name) => {
        const value = String(name || "").trim();
        if (value) names.add(value);
      });
    }
    const loaded = resource?.container_charts;
    if (loaded && typeof loaded === "object" && !Array.isArray(loaded)) {
      Object.keys(loaded).forEach((name) => {
        const value = String(name || "").trim();
        if (value) names.add(value);
      });
    }
    return Array.from(names).sort();
  }

  function resourceContainerSelectionKey(resource) {
    return resource?.resource_id || app.state.selectedResourceId || "";
  }

  function selectedContainerEntry(resource, metricKey) {
    if (!list.isK8s(resource)) return null;
    const entries = containerChartEntries(resource, metricKey);
    if (!entries.length) return null;
    const selectedName = selectedContainerName(resource, metricKey);
    return entries.find((item) => item.name === selectedName)
      || entries[0];
  }

  function selectedContainerName(resource, metricKey) {
    if (!list.isK8s(resource)) return "";
    const names = availableContainerNames(resource);
    if (!names.length) return "";
    const selectedName = app.selectedContainerByResource.get(resourceContainerSelectionKey(resource));
    return names.includes(selectedName) ? selectedName
      : names[0];
  }

  function containerMetricMode(resource, containerName, metricKey) {
    if (!containerName) return "";
    const modes = resource?.container_metric_modes;
    if (!modes || typeof modes !== "object" || Array.isArray(modes)) return "";
    const byMetric = modes[containerName];
    if (!byMetric || typeof byMetric !== "object" || Array.isArray(byMetric)) return "";
    return String(byMetric[metricKey] || "");
  }

  function metricModeForChart(resource, metricKey, containerName = "") {
    return containerMetricMode(resource, containerName || selectedContainerName(resource, metricKey), metricKey)
      || String(resource?.spec?.[`${metricKey}_metric_mode`] || "");
  }

  function isRawMetricMode(metricKey, mode) {
    const value = String(mode || "").toLowerCase();
    if (String(metricKey).startsWith("cpu_")) return value.includes("cpu_usage_cores") || value === "raw";
    if (String(metricKey).startsWith("memory_")) return value.includes("memory_working_set_gb") || value === "raw";
    return false;
  }

  function displayUnitForChart(resource, metricKey, containerName = "") {
    const mode = metricModeForChart(resource, metricKey, containerName);
    if (isRawMetricMode(metricKey, mode)) {
      return String(metricKey).startsWith("cpu_") ? "cores" : "gib";
    }
    return list.resolveDisplayUnit(resource, metricKey);
  }

  function metricTitleForChart(resource, metricKey, containerName = "") {
    const mode = metricModeForChart(resource, metricKey, containerName);
    if (isRawMetricMode(metricKey, mode)) {
      if (String(metricKey).startsWith("cpu_")) return "CPU 使用量";
      if (String(metricKey).startsWith("memory_")) return "内存使用量";
    }
    return list.metricTitleFor(resource, metricKey);
  }

  function activeChartData(resource, metricKey, fallbackChartData) {
    const containerEntry = selectedContainerEntry(resource, metricKey);
    return containerEntry?.chart || fallbackChartData;
  }

  function activeContainerSubtext(resource, metricKey) {
    const containerEntry = selectedContainerEntry(resource, metricKey);
    return containerEntry ? `Container: ${containerEntry.name}` : "";
  }

  function buildChartOption(chartData, metricKey, displayUnit = "percent", resource = null) {
    const bestMethod = chartData.best_method;
    const isVm = resource ? !list.isK8s(resource) : false;
    const xTrain = Array.isArray(chartData.x_train_ms) ? chartData.x_train_ms : [];
    const yTrain = Array.isArray(chartData.y_train) ? chartData.y_train : [];
    const xTest = Array.isArray(chartData.x_test_ms) ? chartData.x_test_ms : [];
    const yTest = Array.isArray(chartData.y_test) ? chartData.y_test : [];
    const xPredFuture = Array.isArray(chartData.x_pred_ms) ? chartData.x_pred_ms : [];
    const anchorTs = xTrain.length ? xTrain[xTrain.length - 1] : null;
    const anchorVal = yTrain.length ? yTrain[yTrain.length - 1] : null;
    const rawTrainPairs = toPairs(xTrain, yTrain);
    const rawTestPairs = toPairs(anchorTs == null ? xTest : [anchorTs].concat(xTest), anchorVal == null ? yTest : [anchorVal].concat(yTest));
    const windowInfo = resolveChartWindow(xTrain, xTest, xPredFuture, isVm);
    const activeMode = chartMode();
    // VM 资源强制显示原始数据模式标题
    const modeLabel = isVm ? "原始" : activeMode.label;
    const isPercentMode = displayUnit === "percent";
    const historyData = prepareSeriesData(rawTrainPairs, windowInfo, isVm);
    const historyBridgePoint = historyData.length ? historyData[historyData.length - 1] : null;
    const testData = prependBridgePoint(
      prepareSeriesData(rawTestPairs, windowInfo, isVm),
      historyBridgePoint
    );
    const series = [{
      name: "历史",
      type: "line",
      data: historyData,
      showSymbol: false,
      sampling: "lttb",
      lineStyle: { color: "#2563eb", width: 1.35, opacity: (isVm || activeMode.key === "raw") ? 0.55 : 0.78 },
      itemStyle: { color: "#2563eb" },
      z: 2,
    }];

    if (app.chartAuxiliaryVisible) {
      const futureStart = xPredFuture.length ? normalizeTsMs(xPredFuture[0]) : null;
      const futureEnd = xPredFuture.length ? normalizeTsMs(xPredFuture[xPredFuture.length - 1]) : null;
      const markLineData = auxiliaryMarkLines(metricKey, isPercentMode);
      const markArea = futureStart != null && futureEnd != null ? {
        silent: true,
        itemStyle: { color: "rgba(217,119,6,.09)" },
        data: [[{ xAxis: futureStart }, { xAxis: futureEnd }]],
      } : undefined;
      if (markLineData.length || markArea) {
        series.push({
          name: "辅助线",
          type: "line",
          data: [],
          silent: true,
          tooltip: { show: false },
          lineStyle: { opacity: 0 },
          markLine: markLineData.length ? {
            silent: true,
            symbol: ["none", "none"],
            label: { show: false },
            data: markLineData,
          } : undefined,
          markArea,
          z: 0,
        });
      }
    }

    series.push({
      name: "测试",
      type: "line",
      data: testData,
      showSymbol: false,
      sampling: "lttb",
      lineStyle: { color: "#dc2626", width: 2.1 },
      itemStyle: { color: "#dc2626" },
      z: 3,
    });

    const legendData = ["历史", "测试"];
    for (const m of Object.keys(chartData.preds || chartData.preds_future || {})) {
      const label = app.labelMap[m] || m;
      const testPred = chartData.preds?.[m];
      const futurePred = chartData.preds_future?.[m];
      let predX = [];
      let predY = [];
      if (anchorTs != null && anchorVal != null) {
        predX.push(anchorTs);
        predY.push(anchorVal);
      }
      if (Array.isArray(testPred) && Array.isArray(chartData.x_test_ms)) {
        predX = predX.concat(chartData.x_test_ms);
        predY = predY.concat(testPred);
      }
      if (Array.isArray(futurePred) && xPredFuture.length) {
        predX = predX.concat(xPredFuture);
        predY = predY.concat(futurePred);
      }
      if (!predX.length) continue;
      const rawPredPairs = toPairs(predX, predY);
      legendData.push(label);
      series.push({
        name: label,
        type: "line",
        data: prepareSeriesData(rawPredPairs, windowInfo, isVm),
        showSymbol: false,
        sampling: "lttb",
        lineStyle: {
          type: "dashed",
          color: app.colorMap[m] || "#64748b",
          width: m === bestMethod ? 2.6 : 1.9,
          opacity: m === bestMethod ? 1 : 0.72,
        },
        itemStyle: { color: app.colorMap[m] || "#64748b" },
        z: 4,
      });
    }

    const bestRmse = chartData.metrics?.[bestMethod]?.rmse;
    const timeAxis = buildTimeAxisConfigFromPairs(series.map((item) => item.data), windowInfo);
    const containerScope = activeContainerSubtext(resource, metricKey) || metricContainerScope(resource, metricKey, displayUnit);
    return {
      backgroundColor: "transparent",
      animation: false,
      title: {
        text: `${metricTitleForChart(resource, metricKey)} 预测 · ${modeLabel}${bestMethod ? ` · 最优 ${app.labelMap[bestMethod] || bestMethod}` : ""}${bestRmse !== undefined ? ` · RMSE ${bestRmse.toFixed(3)}` : ""}`,
        subtext: containerScope,
        left: "center",
        top: 6,
        textStyle: { color: "#0f172a", fontSize: 13, fontWeight: 800 },
        subtextStyle: { color: "#64748b", fontSize: 11, fontWeight: 700, lineHeight: 16 },
        itemGap: 4,
      },
      tooltip: {
        trigger: "axis",
        formatter(params) {
          if (!params || !params.length) return "";
          const t = params[0].value?.[0];
          let html = `${formatMs(t, "full")}<br/>`;
          for (const p of params) {
            const raw = Array.isArray(p.value) ? p.value[1] : p.value;
            const num = Number(raw);
            html += Number.isFinite(num)
              ? `${p.marker}${p.seriesName}: ${list.formatStatValue(num, displayUnit)}<br/>`
              : `${p.marker}${p.seriesName}: ${raw}<br/>`;
          }
          return html;
        },
      },
      legend: { top: containerScope ? 54 : 34, left: 8, data: legendData, itemWidth: 12, itemHeight: 7, textStyle: { color: "#475569", fontSize: 10 } },
      grid: { left: chartGridLeft(displayUnit), right: 18, top: containerScope ? 88 : 70, bottom: 58, containLabel: true },
      xAxis: {
        type: "time",
        min: timeAxis.min,
        max: timeAxis.max,
        splitNumber: 5,
        axisLabel: {
          color: "#64748b",
          hideOverlap: true,
          formatter(value) {
            if (timeAxis.spanMs <= 2 * 86400000) return formatMs(value, "mdhm");
            if (timeAxis.spanMs <= 20 * 86400000) return formatMs(value, "mdh0");
            return formatMs(value, "md");
          },
        },
        splitLine: { show: true, lineStyle: { color: "rgba(15,23,42,.08)" } },
      },
      yAxis: {
        type: "value",
        min: 0,
        axisLabel: {
          color: "#64748b",
          margin: chartYAxisLabelMargin(displayUnit),
          formatter: (v) => {
            if (!Number.isFinite(v)) return "-";
            if (displayUnit === "cores") return `${Math.round(v)} C`;
            if (displayUnit === "gib") return list.formatMemoryGiB(v, 1);
            return `${Math.round(v * 100)}%`;
          },
        },
        splitLine: { lineStyle: { color: "rgba(15,23,42,.08)" } },
      },
      dataZoom: [
        { type: "inside", xAxisIndex: 0, filterMode: "none", zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: true },
        {
          type: "slider",
          xAxisIndex: 0,
          filterMode: "none",
          height: 18,
          bottom: 12,
          borderColor: "rgba(148,163,184,.28)",
          fillerColor: "rgba(37,99,235,.12)",
          handleSize: "80%",
          textStyle: { color: "#64748b", fontSize: 10 },
        },
      ],
      series,
    };
  }

  function rememberResourcePayload(resourceId, payload) {
    app.resourcePayloadCache.delete(resourceId);
    app.resourcePayloadCache.set(resourceId, payload);
    while (app.resourcePayloadCache.size > MAX_RESOURCE_CACHE_ITEMS) {
      const oldest = app.resourcePayloadCache.keys().next().value;
      app.resourcePayloadCache.delete(oldest);
      for (const key of Array.from(app.loadedChartKeys)) {
        if (key.startsWith(`${oldest}:`)) app.loadedChartKeys.delete(key);
      }
    }
  }

  function rememberChartData(key, data) {
    app.chartDataByKey.delete(key);
    app.chartDataByKey.set(key, data);
    while (app.chartDataByKey.size > MAX_CHART_CACHE_ITEMS) {
      const oldest = app.chartDataByKey.keys().next().value;
      app.chartDataByKey.delete(oldest);
      for (const key of Array.from(app.loadedChartKeys)) {
        if (key.startsWith(`${oldest}:`)) app.loadedChartKeys.delete(key);
      }
    }
  }

  function summaryResource(resourceId) {
    return app.state.loadedItems.find((item) => String(item?.resource_id || "") === String(resourceId)) || null;
  }

  async function ensureResource(resourceId) {
    const cached = app.resourcePayloadCache.get(resourceId);
    if (cached?.resource) return cached.resource;
    let req = app.pendingDetailRequests.get(resourceId);
    if (!req) {
      req = api.requestJson(`/api/resources/${encodeURIComponent(resourceId)}?include_charts=false`)
        .then((payload) => {
          const resource = { ...(summaryResource(resourceId) || {}), ...(payload?.resource || {}) };
          const merged = { ...payload, resource };
          rememberResourcePayload(resourceId, merged);
          return merged;
        })
        .finally(() => app.pendingDetailRequests.delete(resourceId));
      app.pendingDetailRequests.set(resourceId, req);
    }
    const payload = await req;
    return payload.resource;
  }

  function chartLoadOptions(resource, metricKey) {
    const selectedRange = chartRange();
    const extended = selectedRange.key === "7d" || selectedRange.key === "all";
    const historyPoints = extended ? MAX_HISTORY_POINTS : DEFAULT_HISTORY_POINTS;
    if (!selectedRange.durationMs || !extended) {
      return { historyPoints, startMs: null, endMs: null };
    }
    const rid = String(resource?.resource_id || "");
    const fallback = app.chartDataByKey.get(`${rid}:${metricKey}`);
    const data = activeChartData(resource, metricKey, fallback);
    const observed = [];
    for (const values of [data?.x_test_ms, data?.x_train_ms]) {
      if (!Array.isArray(values) || !values.length) continue;
      const value = normalizeTsMs(values[values.length - 1]);
      if (Number.isFinite(value)) observed.push(value);
    }
    const endMs = observed.length ? Math.max(...observed) : null;
    return {
      historyPoints,
      startMs: endMs == null ? null : Math.max(0, endMs - selectedRange.durationMs),
      endMs,
    };
  }

  function chartLoadKey(resource, metricKey) {
    const rid = String(resource?.resource_id || "");
    const containerName = selectedContainerName(resource, metricKey) || "workload";
    const options = chartLoadOptions(resource, metricKey);
    return `${rid}:${metricKey}:${containerName}:${options.historyPoints}:${options.startMs ?? "recent"}:${options.endMs ?? "latest"}`;
  }

  function cacheChartPayload(resource, payload, metricKey, requestKey) {
    if (!resource || !payload) return;
    resource.charts = { ...(resource.charts || {}), ...(payload.charts || {}) };
    const incomingContainers = payload.container_charts || {};
    const mergedContainers = { ...(resource.container_charts || {}) };
    Object.entries(incomingContainers).forEach(([name, metrics]) => {
      mergedContainers[name] = { ...(mergedContainers[name] || {}), ...(metrics || {}) };
    });
    resource.container_charts = mergedContainers;
    const chart = resource.charts?.[metricKey];
    if (chart) rememberChartData(`${resource.resource_id}:${metricKey}`, chart);
    app.loadedChartKeys.add(requestKey);
  }

  async function ensureChartData(resource, metricKey) {
    if (!resource || !metricKey) return resource;
    const rid = String(resource.resource_id || "");
    if (!rid) return resource;
    const containerName = selectedContainerName(resource, metricKey);
    const loadOptions = chartLoadOptions(resource, metricKey);
    const requestKey = chartLoadKey(resource, metricKey);
    if (app.loadedChartKeys.has(requestKey)) return resource;
    let req = app.pendingChartRequests.get(requestKey);
    if (!req) {
      const url = api.buildQuery(`/api/resources/${encodeURIComponent(rid)}/charts`, {
        metric: metricKey,
        container: containerName || undefined,
        history_points: loadOptions.historyPoints,
        start_ms: loadOptions.startMs,
        end_ms: loadOptions.endMs,
      });
      req = api.requestJson(url)
        .then((payload) => {
          cacheChartPayload(resource, payload, metricKey, requestKey);
          return resource;
        })
        .finally(() => app.pendingChartRequests.delete(requestKey));
      app.pendingChartRequests.set(requestKey, req);
    }
    return req;
  }

  async function ensureAdviceChartData(resource) {
    if (!resource || !list.isK8s(resource)) return resource;
    await Promise.allSettled(
      list.metricKeysFor(resource).map((metricKey) => ensureChartData(resource, metricKey))
    );
    return resource;
  }

  function disposeDetailChart() {
    if (!app.detailChartInstance) return;
    try { app.detailChartInstance.dispose(); } catch (_) {}
    app.detailChartInstance = null;
  }

  function disposeModalChart() {
    if (!app.modalChartInstance) return;
    try { app.modalChartInstance.dispose(); } catch (_) {}
    app.modalChartInstance = null;
  }

  function chartButton(option, group, extraClass = "") {
    const stateKey = group === "range" ? app.chartRangeKey : app.chartModeKey;
    return `<button type="button" class="${stateKey === option.key ? "active" : ""}${extraClass}" data-chart-${group}="${list.escapeHtml(option.key)}">${list.escapeHtml(option.label)}</button>`;
  }

  function containerSelectorMarkup(resource) {
    const metricKey = app.state.selectedMetricKey;
    const names = metricKey ? availableContainerNames(resource) : [];
    if (!names.length) return "";
    const activeName = selectedContainerName(resource, metricKey) || names[0];
    return `
      <div class="chart-control-group chart-container-group" aria-label="容器选择">
        ${names.map((name) => `
          <button type="button" class="${name === activeName ? "active" : ""}" data-chart-container="${list.escapeHtml(name)}" title="${list.escapeHtml(name)}">${list.escapeHtml(name)}</button>
        `).join("")}
      </div>
    `;
  }

  function chartControlsMarkup(resource) {
    // VM 资源不显示时间范围和显示模式选择器
    if (resource && !list.isK8s(resource)) return "";
    return `
      <div class="chart-control-group" aria-label="图表时间范围">
        ${CHART_RANGES.map((option) => chartButton(option, "range")).join("")}
      </div>
      <div class="chart-control-group" aria-label="图表显示模式">
        ${CHART_MODES.map((option) => chartButton(option, "mode")).join("")}
      </div>
      ${containerSelectorMarkup(resource)}
    `;
  }

  function metricButtonsMarkup(resource, activeMetric, modal = false) {
    const attr = modal ? "data-modal-metric-key" : "data-metric-key";
    return list.metricKeysFor(resource).map((key) => (
      `<button type="button" class="${key === activeMetric ? "active" : ""}" ${attr}="${list.escapeHtml(key)}">${list.escapeHtml(metricTitleForChart(resource, key))}</button>`
    )).join("");
  }

  function renderModalMetricTabs(resource, activeMetric) {
    if (!app.els.chartModalMetricTabs) return;
    app.els.chartModalMetricTabs.innerHTML = `
      <div class="metric-tab-group">${metricButtonsMarkup(resource, activeMetric, true)}</div>
      ${chartControlsMarkup(resource)}
    `;
  }

  function renderSpec(resource) {
    const spec = resource?.spec || {};
    // 原始值为 null/undefined/"" 时返回 null，让外层 filter 能识别并隐藏该字段
    // 只有真实存在的值才格式化为 "数字 单位"
    const formatMaybe = (value, unit, digits = 2) => {
      if (value === undefined || value === null || value === "") return null;
      const text = list.formatNumber(value, digits);
      return text === "-" ? null : `${text} ${unit}`;
    };
    const metricModeLabel = (mode, resourceLabel) => {
      const value = String(mode || "").toLowerCase();
      if (value.includes(`/${resourceLabel}_limit`)) return "Limit 使用率";
      if (value.includes(`/${resourceLabel}_request`)) return "Request 使用率（无上限，仅评估低使用）";
      if (value.includes("_cores")) return "CPU 绝对值";
      if (value.includes("_gb")) return "内存绝对值";
      return mode;
    };
    const containerSpecMarkup = () => {
      const containers = spec.containers && typeof spec.containers === "object" && !Array.isArray(spec.containers)
        ? spec.containers
        : {};
      const names = Object.keys(containers).sort();
      if (!names.length) return "";
      const rows = names.map((name) => {
        const item = containers[name] && typeof containers[name] === "object" ? containers[name] : {};
        return `
          <div class="container-spec-row">
            <strong title="${list.escapeHtml(name)}">${list.escapeHtml(name)}</strong>
            <span>${list.escapeHtml(formatMaybe(item.cpu_request_cores, "C") || "-")}</span>
            <span>${list.escapeHtml(formatMaybe(item.cpu_limit_cores, "C") || "-")}</span>
            <span>${list.escapeHtml(item.memory_request_gb == null ? "-" : list.formatMemoryGiB(item.memory_request_gb))}</span>
            <span>${list.escapeHtml(item.memory_limit_gb == null ? "-" : list.formatMemoryGiB(item.memory_limit_gb))}</span>
          </div>`;
      }).join("");
      return `
        <div class="container-spec-grid">
          <div class="container-spec-title">容器规格</div>
          <div class="container-spec-head">
            <span>Container</span><span>CPU Request</span><span>CPU Limit</span><span>内存 Request</span><span>内存 Limit</span>
          </div>
          ${rows}
          <div class="container-spec-note">Request 为 Kubernetes / Prometheus 的生效值；只配置 limit 时，Kubernetes 可能将 request 默认成 limit。</div>
        </div>`;
    };
    const entries = list.isK8s(resource)
      ? [
          ["集群", spec.cluster],
          ["Namespace", spec.namespace],
          ["Workload", [spec.workload_kind || spec.owner_kind, spec.workload_name || spec.owner_name].filter(Boolean).join("/")],
          ["副本数", spec.replicas],
          ["观测 Pod 数", spec.replicas_observed],
          ["容器", Array.isArray(spec.containers_observed) ? spec.containers_observed.join(", ") : spec.container],
          ["节点", Array.isArray(spec.nodes) ? spec.nodes.join(", ") : spec.node],
          ["CPU 扩容基准", metricModeLabel(spec.cpu_limit_metric_mode, "cpu")],
          ["CPU 缩容基准", metricModeLabel(spec.cpu_request_metric_mode, "cpu")],
          ["内存扩容基准", metricModeLabel(spec.memory_limit_metric_mode, "memory")],
          ["内存缩容基准", metricModeLabel(spec.memory_request_metric_mode, "memory")],
        ]
      : [
          ["集群", spec.cluster],
          ["IP", spec.ip],
          ["CPU", formatMaybe(spec.cpu_cores, "核", 0)],
          ["内存", formatMaybe(spec.memory_gb, "GB", 0)],
          ["磁盘", formatMaybe(spec.disk_gb, "GB", 0)],
        ];
    const summaryMarkup = entries
      .filter(([, value]) =>
        value !== undefined && value !== null && String(value).trim() !== "" && String(value) !== "-"
      )
      .map(([label, value]) => `<div class="spec-item"><span>${list.escapeHtml(label)}</span><strong title="${list.escapeHtml(value)}">${list.escapeHtml(value)}</strong></div>`)
      .join("");
    app.els.detailSpec.innerHTML = summaryMarkup + (list.isK8s(resource) ? containerSpecMarkup() : "");
  }

  function displayResourceId(resource, fallbackResourceId = "") {
    const raw = String(resource?.resource_id || fallbackResourceId || "");
    if (!list.isK8s(resource) && !raw.startsWith("k8s:")) return raw;
    return raw.replace(/^k8s:/, "").split(":").filter(Boolean).join(" / ");
  }

  function displayDetailTitle(resource, fallbackResourceId = "") {
    if (resource && list.isK8s(resource)) return list.titleFor(resource);
    const raw = String(resource?.resource_id || fallbackResourceId || "");
    if (raw.startsWith("k8s:")) {
      const parts = raw.split(":").filter(Boolean);
      return parts[parts.length - 1] || displayResourceId(resource, fallbackResourceId);
    }
    return raw || "-";
  }

  function confidenceTooltip(resource) {
    const advice = resource?.scaling_advice || {};
    const score = Number(advice.confidence_score);
    if (!Number.isFinite(score)) return list.CONFIDENCE_HELP;
    const metricScores = advice.confidence_metric_scores && typeof advice.confidence_metric_scores === "object"
      ? advice.confidence_metric_scores
      : {};
    const values = Object.values(metricScores).map((value) => Number(value)).filter(Number.isFinite);
    if (!values.length) return `置信度${list.formatNumber(score, 1)} = 默认中等置信度${list.formatNumber(score, 1)} + 其他调整0`;
    const primary = Math.max(...values);
    const avg = values.reduce((acc, value) => acc + value, 0) / values.length;
    const multiMetricBonus = values.length >= 2 ? 4 : 0;
    const mixedSignalPenalty = advice.has_mixed_signals ? -8 : 0;
    const executionReadyBonus = list.isK8s(resource) && advice.target_k8s_policy?.ready_for_execution ? 4 : 0;
    const components = [
      { label: "最高指标得分", rawValue: primary, multiplier: 0.65, value: 0.65 * primary },
      { label: "平均指标得分", rawValue: avg, multiplier: 0.35, value: 0.35 * avg },
      { label: "多指标加成", value: multiMetricBonus },
      { label: "混合信号扣分", value: mixedSignalPenalty },
    ];
    if (list.isK8s(resource)) {
      components.push({ label: "执行就绪加成", value: executionReadyBonus });
    }
    const subtotal = components.reduce((acc, part) => acc + Number(part.value || 0), 0);
    const residual = score - subtotal;
    components.push({ label: "其他调整/封顶", value: residual });
    const formula = `置信度${list.formatNumber(score, 1)} = ${components.map((part, index) => formulaTerm(part, index)).join(" ")}`;
    const lines = [formula, "指标得分:"];
    Object.keys(metricScores).sort().forEach((metric) => {
      const value = Number(metricScores[metric]);
      if (!Number.isFinite(value)) return;
      const action = advice.metric_actions?.[metric] || "";
      const label = app.metricTitleMap[metric] || metric;
      const actionText = action ? ` ${list.actionLabel(action)}` : "";
      lines.push(`  ${label}${actionText}: ${list.formatNumber(value, 1)}`);
    });
    return lines.join("\n");
  }

  function formulaTerm(part, index) {
    const value = Number(part.value);
    const rawValue = Number(part.rawValue);
    const multiplier = Number(part.multiplier);
    const text = Number.isFinite(rawValue) && Number.isFinite(multiplier)
      ? `${part.label}${list.formatNumber(rawValue, 1)} * ${list.formatNumber(multiplier, 2)}`
      : `${part.label}${list.formatNumber(Math.abs(value), 1)}`;
    if (index === 0) return value < 0 ? `-${text}` : text;
    return `${value < 0 ? "-" : "+"} ${text}`;
  }

  function renderAdvice(resource) {
    const advice = resource?.scaling_advice || {};
    const action = list.actionOf(resource);
    const confidence = list.confidenceOf(resource);
    const actionText = list.actionLabel(action);
    const analysisReasons = list.analysisOnlyReasons(resource);
    const analysisReasonMarkup = analysisReasons.length ? `
        <div class="analysis-only-reasons">
          <span>仅分析原因</span>
          <ul>
            ${analysisReasons.map((reason) => `<li>${list.escapeHtml(reason)}</li>`).join("")}
          </ul>
        </div>` : "";
    const historyLabel = list.historyCoverageLabel(resource);
    app.els.detailConfidence.innerHTML = `置信度 ${list.escapeHtml(list.CONFIDENCE_LABELS[confidence] || confidence)}${advice.confidence_score ? ` · ${list.formatNumber(advice.confidence_score, 1)}分` : ""}${historyLabel ? ` · ${list.escapeHtml(historyLabel)}` : ""} ${list.infoTooltip(confidenceTooltip(resource), "置信度计算说明")}`;
    app.els.detailConfidence.className = `confidence-chip is-${confidence}`;
    app.els.detailAdvice.innerHTML = `
      <div class="decision-summary is-${list.escapeHtml(action)}">
        <div class="decision-row">
          <span class="decision-label">建议动作</span>
          <span class="decision-action">${list.escapeHtml(actionText)}</span>
        </div>
        <div class="decision-row">
          <span class="decision-label">目标结果</span>
          <div class="target-result" title="${list.escapeHtml(list.targetSpecText(resource))}">
            ${list.targetSpecDetailMarkup(resource)}
          </div>
        </div>
        ${analysisReasonMarkup}
      </div>
      <div class="reason-grid">
        ${list.metricKeysFor(resource).map((key) => {
          const containerName = selectedContainerName(resource, key);
          const observed = list.containerMetricObservedStatsFor(resource, key, containerName)
            || list.metricObservedStatsFor(resource, key);
          const mAction = containerName
            ? list.containerMetricActionFor(resource, key, containerName)
            : list.metricActionFor(resource, key);
          const unit = displayUnitForChart(resource, key, containerName);
          const st = observed || {};
          const metricTitle = `${metricTitleForChart(resource, key, containerName)}${containerName ? ` · ${containerName}` : ""}`;
          const statScope = list.isK8s(resource) ? (containerName ? "Container" : "Workload") : "Resource";
          return `<div class="reason-item">
            <span class="reason-metric">${list.escapeHtml(metricTitle)}</span>
            <strong class="reason-action is-${list.escapeHtml(mAction)}">${list.escapeHtml(list.actionLabel(mAction))}</strong>
            <small class="reason-stats">
              <span><b>平均</b><em>${list.formatStatValue(st.avg, unit)}</em></span>
              <span><b>P95 · ${list.escapeHtml(statScope)}</b><em>${list.formatStatValue(st.p95, unit)}</em></span>
              <span><b>峰值</b><em>${list.formatStatValue(st.peak, unit)}</em></span>
            </small>
          </div>`;
        }).join("")}
      </div>`;

    app.els.detailActions.innerHTML = window.ScalingUI.buildControls(
      resource.resource_id,
      action,
      confidence,
      !!advice.has_mixed_signals,
      { analysisOnly: Boolean(advice.analysis_only), resourceType: list.resourceTypeOf(resource), resource }
    );
  }

  function renderMetricTabs(resource, activeMetric) {
    app.els.metricTabs.innerHTML = `
      <div class="metric-tab-group">${metricButtonsMarkup(resource, activeMetric)}</div>
      ${chartControlsMarkup(resource)}
    `;
  }

  async function renderChart(resourceId, metricKey, resource = null) {
    disposeDetailChart();
    app.els.detailChart.innerHTML = `<div class="chart-state">正在加载图表...</div>`;
    try {
      resource = resource || await ensureResource(resourceId);
      await ensureChartData(resource, metricKey);
    } catch (e) {
      if (app.state.selectedResourceId === resourceId) {
        app.els.detailChart.innerHTML = `
          <div class="chart-state is-error">
            图表加载失败：${list.escapeHtml(e.message || e)}
            <button type="button" class="link-btn" data-chart-retry>重试</button>
          </div>`;
      }
      return false;
    }
    if (app.state.selectedResourceId !== resourceId) return;
    app.els.detailChart.innerHTML = "";
    const key = `${resourceId}:${metricKey}`;
    const fallbackData = app.chartDataByKey.get(key);
    const data = activeChartData(resource, metricKey, fallbackData);
    if (!data) {
      app.els.detailChart.innerHTML = `<div class="chart-state">暂无 ${list.escapeHtml(metricTitleForChart(resource, metricKey))} 图表数据</div>`;
      return false;
    }
    if (typeof echarts === "undefined") {
      app.els.detailChart.innerHTML = `<div class="chart-state is-error">ECharts 未加载</div>`;
      return false;
    }
    const displayUnit = displayUnitForChart(resource, metricKey);
    app.detailChartInstance = echarts.init(app.els.detailChart, null, { renderer: app.ECHARTS_RENDERER });
    app.detailChartInstance.setOption(buildChartOption(data, metricKey, displayUnit, resource), { notMerge: true, lazyUpdate: false });
    requestAnimationFrame(() => app.detailChartInstance?.resize());
    return true;
  }

  async function openChartModal(metricKey) {
    if (!app.state.selectedResourceId || !app.state.selectedMetricKey) return;
    const resource = await ensureResource(app.state.selectedResourceId);
    const metricKeys = list.metricKeysFor(resource);
    let activeMetric = metricKey || app.state.selectedMetricKey || list.triggerMetric(resource);
    if (!metricKeys.includes(activeMetric)) activeMetric = list.triggerMetric(resource);
    app.state.selectedMetricKey = activeMetric;
    try {
      await ensureChartData(resource, activeMetric);
    } catch (_) {
      await renderChart(app.state.selectedResourceId, activeMetric, resource);
      return;
    }
    const key = `${app.state.selectedResourceId}:${activeMetric}`;
    const fallbackData = app.chartDataByKey.get(key);
    const data = activeChartData(resource, activeMetric, fallbackData);
    if (!data || typeof echarts === "undefined" || !app.els.chartModal || !app.els.chartModalChart) return;
    disposeModalChart();
    app.els.chartModal.hidden = false;
    const metricName = metricTitleForChart(resource, activeMetric);
    app.els.chartModalTitle.textContent = `${metricName} 指标预测`;
    app.els.chartModalSubtitle.textContent = app.state.selectedResourceId;
    renderMetricTabs(resource, activeMetric);
    renderModalMetricTabs(resource, activeMetric);
    app.els.chartModalChart.innerHTML = "";
    app.modalChartInstance = echarts.init(app.els.chartModalChart, null, { renderer: app.ECHARTS_RENDERER });
    const displayUnit = displayUnitForChart(resource, activeMetric);
    app.modalChartInstance.setOption(buildChartOption(data, activeMetric, displayUnit, resource), { notMerge: true, lazyUpdate: false });
    renderChart(app.state.selectedResourceId, activeMetric, resource);
    requestAnimationFrame(() => app.modalChartInstance?.resize());
  }

  function closeChartModal() {
    disposeModalChart();
    if (app.els.chartModal) app.els.chartModal.hidden = true;
  }

  async function renderDetail(resourceId, metricKey) {
    app.els.detailEmpty.hidden = true;
    app.els.detailContent.hidden = false;
    app.els.detailPanel.classList.add("is-open");
    app.els.detailTitle.textContent = displayDetailTitle(null, resourceId);
    app.els.detailTitle.title = resourceId;
    app.els.detailSubtitle.textContent = "正在加载详情...";
    app.els.detailAdvice.innerHTML = `<div class="chart-state">正在加载详情...</div>`;
    const initial = summaryResource(resourceId);
    if (initial) {
      app.els.detailType.textContent = list.typeLabel(initial);
      app.els.detailTitle.textContent = displayDetailTitle(initial, resourceId);
      app.els.detailSubtitle.textContent = list.subtitleFor(initial);
      renderAdvice(initial);
      renderSpec(initial);
    }
    try {
      const resource = await ensureResource(resourceId);
      if (app.state.selectedResourceId !== resourceId) return;
      if (!resource || resource.prediction_pending) {
        app.els.detailSubtitle.textContent = "预测更新中";
        app.els.detailAdvice.innerHTML = `<div class="chart-state">该资源正在更新预测，请稍后刷新。</div>`;
        return;
      }
      const metricKeys = list.metricKeysFor(resource);
      let activeMetric = metricKey || app.state.selectedMetricKey || list.triggerMetric(resource);
      if (!metricKeys.includes(activeMetric)) activeMetric = list.triggerMetric(resource);
      app.state.selectedMetricKey = activeMetric;
      app.els.detailType.textContent = list.typeLabel(resource);
      app.els.detailTitle.textContent = displayDetailTitle(resource, resourceId);
      app.els.detailTitle.title = resource.resource_id || resourceId;
      app.els.detailSubtitle.textContent = list.subtitleFor(resource);
      renderAdvice(resource);
      renderSpec(resource);
      renderMetricTabs(resource, activeMetric);
      const adviceDataPromise = ensureAdviceChartData(resource);
      const loaded = await renderChart(resource.resource_id || resourceId, activeMetric, resource);
      if (loaded && app.state.selectedResourceId === resourceId) renderMetricTabs(resource, activeMetric);
      await adviceDataPromise;
      if (app.state.selectedResourceId === resourceId) renderAdvice(resource);
    } catch (e) {
      app.els.detailSubtitle.textContent = initial ? "详情补充信息加载失败" : "加载失败";
      if (!initial) {
        app.els.detailAdvice.innerHTML = `<div class="chart-state is-error">详情加载失败：${list.escapeHtml(e.message || e)}</div>`;
      }
    }
  }

  async function toggleChartAuxiliary() {
    app.chartAuxiliaryVisible = !app.chartAuxiliaryVisible;
    app.els.chartGuideBtn.textContent = app.chartAuxiliaryVisible ? "辅助线：开" : "辅助线：关";
    app.els.chartGuideBtn.setAttribute("aria-pressed", app.chartAuxiliaryVisible ? "true" : "false");
    if (app.state.selectedResourceId && app.state.selectedMetricKey) {
      const resource = await ensureResource(app.state.selectedResourceId);
      renderChart(app.state.selectedResourceId, app.state.selectedMetricKey, resource);
      if (app.els.chartModal && !app.els.chartModal.hidden) openChartModal();
    }
  }

  async function refreshChartDisplayControls() {
    if (!app.state.selectedResourceId || !app.state.selectedMetricKey) return;
    const resource = await ensureResource(app.state.selectedResourceId);
    renderAdvice(resource);
    renderMetricTabs(resource, app.state.selectedMetricKey);
    const resourceId = app.state.selectedResourceId;
    const selectedContainer = selectedContainerName(resource, app.state.selectedMetricKey);
    const adviceDataPromise = ensureAdviceChartData(resource);
    await renderChart(resourceId, app.state.selectedMetricKey, resource);
    await adviceDataPromise;
    if (
      app.state.selectedResourceId === resourceId
      && selectedContainerName(resource, app.state.selectedMetricKey) === selectedContainer
    ) {
      renderAdvice(resource);
    }
    if (app.els.chartModal && !app.els.chartModal.hidden) {
      renderModalMetricTabs(resource, app.state.selectedMetricKey);
      await openChartModal(app.state.selectedMetricKey);
    }
  }

  function handleChartControlClick(event) {
    const rangeBtn = event.target.closest("button[data-chart-range]");
    const modeBtn = event.target.closest("button[data-chart-mode]");
    const containerBtn = event.target.closest("button[data-chart-container]");
    if (!rangeBtn && !modeBtn && !containerBtn) return false;
    event.preventDefault();
    event.stopPropagation();
    if (rangeBtn) app.chartRangeKey = rangeBtn.dataset.chartRange || app.chartRangeKey;
    if (modeBtn) app.chartModeKey = modeBtn.dataset.chartMode || app.chartModeKey;
    if (containerBtn && app.state.selectedMetricKey) {
      app.selectedContainerByResource.set(
        app.state.selectedResourceId || "",
        containerBtn.dataset.chartContainer || ""
      );
    }
    refreshChartDisplayControls();
    return true;
  }

  window.addEventListener("resize", () => {
    app.detailChartInstance?.resize();
    app.modalChartInstance?.resize();
  });

  app.els.metricTabs?.addEventListener("click", handleChartControlClick);

  app.els.detailChart?.addEventListener("click", (event) => {
    if (!event.target.closest("[data-chart-retry]")) return;
    if (app.state.selectedResourceId && app.state.selectedMetricKey) {
      const payload = app.resourcePayloadCache.get(app.state.selectedResourceId);
      const resource = payload?.resource;
      if (resource) app.loadedChartKeys.delete(chartLoadKey(resource, app.state.selectedMetricKey));
      renderChart(app.state.selectedResourceId, app.state.selectedMetricKey, resource || null);
    }
  });

  app.els.chartModal?.addEventListener("click", (event) => {
    if (event.target.closest("[data-chart-modal-dismiss]")) closeChartModal();
  });

  app.els.chartModalMetricTabs?.addEventListener("click", (event) => {
    if (handleChartControlClick(event)) return;
    const btn = event.target.closest("button[data-modal-metric-key]");
    if (!btn) return;
    openChartModal(btn.dataset.modalMetricKey || app.state.selectedMetricKey);
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && app.els.chartModal && !app.els.chartModal.hidden) closeChartModal();
  });

  window.ResourceCharts = {
    closeChartModal,
    disposeDetailChart,
    openChartModal,
    renderDetail,
    toggleChartAuxiliary,
  };
})();
