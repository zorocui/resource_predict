(function () {
  const app = window.ResourcePredictApp;
  const api = window.ResourceApi;
  const CHART_TIME_ZONE = "UTC";

  function toPairs(xMs, y) {
    const res = [];
    for (let i = 0; i < xMs.length; i++) {
      let ts = xMs[i];
      if (typeof ts === "number" && ts < 1e12) ts = ts * 1000;
      res.push([ts, y[i]]);
    }
    return res;
  }

  function normalizeTsMs(t) {
    if (typeof t !== "number" || !Number.isFinite(t)) return NaN;
    return t < 1e12 ? t * 1000 : t;
  }

  function formatMsInTimeZone(ms, variant) {
    const d = new Date(ms);
    if (variant === "full") {
      const parts = new Intl.DateTimeFormat("en-GB", {
        timeZone: CHART_TIME_ZONE,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).formatToParts(d);
      const pick = (type) => parts.find((x) => x.type === type)?.value || "";
      return `${pick("month")}-${pick("day")} ${pick("hour")}:${pick("minute")}:${pick("second")}`;
    }
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: CHART_TIME_ZONE,
      month: "2-digit",
      day: "2-digit",
      hour: variant === "md" ? undefined : "2-digit",
      minute: variant === "mdhm" || variant === "mdh0" ? "2-digit" : undefined,
      hour12: false,
    }).formatToParts(d);
    const pick = (type) => parts.find((x) => x.type === type)?.value || "";
    const mm = pick("month");
    const dd = pick("day");
    if (variant === "md") return `${mm}-${dd}`;
    const hh = pick("hour") || "00";
    if (variant === "mdh0") return `${mm}-${dd} ${hh}:00`;
    return `${mm}-${dd} ${hh}:${pick("minute") || "00"}`;
  }

  function medianStepMs(msSorted) {
    const diffs = [];
    for (let i = 1; i < msSorted.length; i++) {
      const d = msSorted[i] - msSorted[i - 1];
      if (d > 0) diffs.push(d);
    }
    if (!diffs.length) return null;
    diffs.sort((a, b) => a - b);
    return diffs[Math.floor(diffs.length / 2)];
  }

  function buildTimeAxisConfig(xTrain, xTest, xPredFuture) {
    const bucket = [];
    const pushArr = (arr) => {
      if (!Array.isArray(arr)) return;
      for (const t of arr) {
        const ms = normalizeTsMs(t);
        if (Number.isFinite(ms)) bucket.push(ms);
      }
    };
    pushArr(xTrain);
    pushArr(xTest);
    pushArr(xPredFuture);
    if (!bucket.length) return { min: undefined, max: undefined, minInterval: undefined, spanMs: 0 };
    bucket.sort((a, b) => a - b);
    const minT = bucket[0];
    const maxT = bucket[bucket.length - 1];
    const span = maxT - minT;
    const pad = Math.max(span * 0.02, 60 * 1000);
    const med = medianStepMs(bucket);
    let minInterval;
    if (span <= 7 * 86400000 && med != null && med >= 30 * 1000 && med <= 7 * 86400000) {
      minInterval = med;
    }
    return { min: minT - pad, max: maxT + pad, minInterval, spanMs: span };
  }

  function formatAxisUsagePct(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return String(v);
    return `${(n * 100).toFixed(0)}%`;
  }

  function metricThresholds(metricKey) {
    return {
      upper: { value: 0.8, label: `${app.metricTitleMap[metricKey] || metricKey} 扩容阈值` },
      lower: { value: 0.2, label: `${app.metricTitleMap[metricKey] || metricKey} 缩容阈值` },
    };
  }

  function buildChartOption(chartData, metricKey) {
    const bestMethod = chartData.best_method;
    const metrics = chartData.metrics;
    const xTrain = Array.isArray(chartData.x_train_ms) ? chartData.x_train_ms : [];
    const yTrain = Array.isArray(chartData.y_train) ? chartData.y_train : [];
    const xTest = Array.isArray(chartData.x_test_ms) ? chartData.x_test_ms : [];
    const yTest = Array.isArray(chartData.y_test) ? chartData.y_test : [];
    const xPredFuture = Array.isArray(chartData.x_pred_ms) ? chartData.x_pred_ms : [];
    const anchorTs = xTrain.length ? xTrain[xTrain.length - 1] : null;
    const anchorVal = yTrain.length ? yTrain[yTrain.length - 1] : null;
    const series = [{
      name: "train",
      type: "line",
      data: toPairs(xTrain, yTrain),
      showSymbol: false,
      lineStyle: { color: "#2563eb", width: 2.6 },
      itemStyle: { color: "#2563eb" },
      z: 1,
    }];

    if (app.chartAuxiliaryVisible) {
      const futureStart = xPredFuture.length ? normalizeTsMs(xPredFuture[0]) : null;
      const futureEnd = xPredFuture.length ? normalizeTsMs(xPredFuture[xPredFuture.length - 1]) : null;
      const thresholds = metricThresholds(metricKey);
      series.push({
        name: "辅助线",
        type: "line",
        data: [],
        silent: true,
        tooltip: { show: false },
        lineStyle: { opacity: 0 },
        markLine: {
          silent: true,
          symbol: ["none", "none"],
          label: { show: false },
          data: [
            { yAxis: thresholds.upper.value, name: thresholds.upper.label, lineStyle: { color: "rgba(239,68,68,0.62)", type: "dashed", width: 1.2 } },
            { yAxis: thresholds.lower.value, name: thresholds.lower.label, lineStyle: { color: "rgba(16,185,129,0.62)", type: "dashed", width: 1.2 } },
          ],
        },
        markArea: futureStart != null && futureEnd != null ? {
          silent: true,
          itemStyle: { color: "rgba(245,158,11,0.08)" },
          label: { show: false },
          data: [[{ xAxis: futureStart }, { xAxis: futureEnd }]],
        } : undefined,
        z: 0,
      });
    }

    series.push({
      name: "test",
      type: "line",
      data: toPairs(anchorTs == null ? xTest : [anchorTs].concat(xTest), anchorVal == null ? yTest : [anchorVal].concat(yTest)),
      showSymbol: false,
      lineStyle: { color: "#dc2626", width: 2.8 },
      itemStyle: { color: "#dc2626" },
      z: 2,
    });

    const enabledMethods = Object.keys(chartData.preds || chartData.preds_future || {});
    const legendData = ["train", "test"];
    for (const m of enabledMethods) {
      const methodLabel = `${app.labelMap[m] || m}`;
      legendData.push(methodLabel);
      const testPred = chartData.preds?.[m];
      const futurePred = chartData.preds_future?.[m];
      let predX = [];
      let predY = [];
      if (anchorTs != null && anchorVal != null) {
        predX.push(anchorTs);
        predY.push(anchorVal);
      }
      if (Array.isArray(testPred) && testPred.length && Array.isArray(chartData.x_test_ms)) {
        predX = predX.concat(chartData.x_test_ms);
        predY = predY.concat(testPred);
      }
      if (Array.isArray(futurePred) && futurePred.length && xPredFuture.length) {
        predX = predX.concat(xPredFuture);
        predY = predY.concat(futurePred);
      }
      if (!predX.length || !predY.length) continue;
      const width = m === bestMethod ? 3 : 2;
      const opacity = m === bestMethod ? 1 : 0.75;
      series.push({
        name: methodLabel,
        type: "line",
        data: toPairs(predX, predY),
        showSymbol: false,
        lineStyle: { type: "dashed", color: app.colorMap[m] || "#60a5fa", width, opacity },
        itemStyle: { color: app.colorMap[m] || "#60a5fa", opacity },
        z: 3,
      });
    }

    const bestRmse = metrics?.[bestMethod]?.rmse;
    const titleText = `最准确：${app.labelMap[bestMethod] || bestMethod}` +
      (bestRmse !== undefined ? ` (RMSE=${bestRmse.toFixed(3)})` : "");
    const timeAxis = buildTimeAxisConfig(xTrain, xTest, xPredFuture);
    const axisBottom = timeAxis.spanMs > 14 * 86400000 ? 48 : 40;

    return {
      backgroundColor: "transparent",
      animation: false,
      title: { text: titleText, left: "center", top: 8, textStyle: { color: "rgba(15,23,42,0.95)", fontSize: 12, fontWeight: "bold" } },
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || !params.length) return "";
          const t = params[0].value?.[0];
          let html = `${formatMsInTimeZone(t, "full")}<br/>`;
          for (const p of params) {
            const raw = Array.isArray(p.value) ? p.value[1] : p.value;
            const num = Number(raw);
            html += Number.isFinite(num)
              ? `${p.marker}${p.seriesName}: ${(num * 100).toFixed(2)}%<br/>`
              : `${p.marker}${p.seriesName}: ${raw}<br/>`;
          }
          return html;
        },
      },
      legend: { top: 38, left: "left", data: legendData, icon: "roundRect", itemWidth: 12, itemHeight: 6, itemGap: 12, textStyle: { color: "rgba(15,23,42,0.75)", fontSize: 9 } },
      grid: { left: 48, right: 18, top: 70, bottom: axisBottom },
      xAxis: {
        type: "time",
        min: timeAxis.spanMs > 0 ? timeAxis.min : undefined,
        max: timeAxis.spanMs > 0 ? timeAxis.max : undefined,
        minInterval: timeAxis.minInterval,
        splitNumber: 6,
        axisLabel: {
          color: "rgba(15,23,42,0.70)",
          rotate: timeAxis.spanMs > 20 * 86400000 ? 22 : 0,
          margin: 12,
          hideOverlap: true,
          formatter: function (value) {
            if (timeAxis.spanMs <= 2 * 86400000) return formatMsInTimeZone(value, "mdhm");
            if (timeAxis.spanMs <= 20 * 86400000) return formatMsInTimeZone(value, "mdh0");
            return formatMsInTimeZone(value, "md");
          },
        },
        axisTick: { show: true, lineStyle: { color: "rgba(15,23,42,0.12)" } },
        splitLine: { show: true, lineStyle: { color: "rgba(15,23,42,0.08)" } },
      },
      yAxis: { type: "value", min: 0, axisLabel: { color: "rgba(15,23,42,0.70)", formatter: formatAxisUsagePct } },
      series,
    };
  }

  function buildModalChartOption(chartData, metricKey) {
    const o = buildChartOption(chartData, metricKey);
    const ts = (base, add) => (typeof base === "number" && Number.isFinite(base) ? base + add : base);
    if (o.title?.textStyle) o.title.textStyle = { ...o.title.textStyle, fontSize: ts(o.title.textStyle.fontSize, 2) || 14 };
    if (o.legend?.textStyle) o.legend.textStyle = { ...o.legend.textStyle, fontSize: ts(o.legend.textStyle.fontSize, 2) || 11 };
    if (o.xAxis?.axisLabel && typeof o.xAxis.axisLabel === "object") o.xAxis.axisLabel = { ...o.xAxis.axisLabel, fontSize: ts(o.xAxis.axisLabel.fontSize, 1) };
    if (o.yAxis?.axisLabel && typeof o.yAxis.axisLabel === "object") o.yAxis.axisLabel = { ...o.yAxis.axisLabel, fontSize: ts(o.yAxis.axisLabel.fontSize, 1) };
    if (o.grid && typeof o.grid === "object") {
      o.grid = { ...o.grid, left: ts(o.grid.left, 8) || 56, right: ts(o.grid.right, 8) || 26, top: ts(o.grid.top, 6) || 76, bottom: ts(o.grid.bottom, 10) || 50 };
    }
    return o;
  }

  function cacheResourceChartsFromPayload(payload, fallbackResourceId) {
    const resource = payload?.resource;
    if (!resource) return;
    const rid = String(resource.resource_id || fallbackResourceId || "");
    if (!rid) return;
    app.resourcePayloadCache.set(rid, payload);
    const charts = resource.charts || {};
    for (const mk of ["cpu", "memory", "disk"]) {
      if (charts[mk]) app.chartDataByKey.set(`${rid}:${mk}`, charts[mk]);
    }
  }

  function cacheResourceChartsFromResource(resource) {
    if (!resource || resource.prediction_pending) return;
    cacheResourceChartsFromPayload({ resource }, resource.resource_id);
  }

  async function prefetchResourceDetails(items) {
    const ids = (items || [])
      .map((item) => String(item?.resource_id || ""))
      .filter((rid) => rid && !app.resourcePayloadCache.has(rid));
    if (!ids.length) return;
    const generation = ++app.prefetchGeneration;
    try {
      const payload = await api.requestJson(api.buildQuery("/api/resources/details", { ids: ids.join(",") }), 1);
      if (generation !== app.prefetchGeneration) return;
      for (const resource of payload.resources || []) cacheResourceChartsFromResource(resource);
    } catch (_) {
      // Lazy per-resource loading remains the fallback.
    }
  }

  function disposeModalChart() {
    if (!app.modalChartInstance) return;
    try {
      app.modalChartInstance.dispose();
    } catch (_) {
      // noop
    }
    app.modalChartInstance = null;
    app.modalChartContext = null;
  }

  function closeChartModal() {
    if (app.chartModalEl) app.chartModalEl.hidden = true;
    disposeModalChart();
    if (app.chartModalCanvasEl) app.chartModalCanvasEl.innerHTML = "";
  }

  async function ensureChartData(resourceId, metricKey) {
    const key = `${resourceId}:${metricKey}`;
    let data = app.chartDataByKey.get(key);
    if (data) return data;
    const cachedPayload = app.resourcePayloadCache.get(resourceId);
    if (cachedPayload) {
      cacheResourceChartsFromPayload(cachedPayload, resourceId);
      data = app.chartDataByKey.get(key);
      if (data) return data;
    }
    const payload = await api.requestJson(`/api/resources/${encodeURIComponent(resourceId)}`);
    cacheResourceChartsFromPayload(payload, resourceId);
    return app.chartDataByKey.get(key) || null;
  }

  async function openChartModal(resourceId, metricKey) {
    if (typeof echarts === "undefined" || !app.chartModalEl || !app.chartModalCanvasEl) return;
    try {
      const chartData = await ensureChartData(resourceId, metricKey);
      if (!chartData) return;
      const mLabel = app.metricTitleMap[metricKey] || metricKey;
      if (app.chartModalTitleEl) app.chartModalTitleEl.textContent = `${resourceId} · ${mLabel} 预测`;
      disposeModalChart();
      app.chartModalCanvasEl.innerHTML = "";
      app.modalChartInstance = echarts.init(app.chartModalCanvasEl, null, { renderer: app.ECHARTS_RENDERER });
      app.modalChartInstance.setOption(buildModalChartOption(chartData, metricKey), { notMerge: true, lazyUpdate: false });
      app.modalChartContext = { resourceId, metricKey };
      app.chartModalEl.hidden = false;
      requestAnimationFrame(() => app.modalChartInstance?.resize());
    } catch (_) {
      closeChartModal();
    }
  }

  function showEchartsError() {
    if (typeof echarts !== "undefined") return false;
    const c = document.querySelector(".container");
    if (c) {
      const box = document.createElement("div");
      box.className = "row";
      box.style.borderColor = "rgba(239, 68, 68, 0.45)";
      box.innerHTML = `
        <div style="color: rgba(239, 68, 68, 0.95); font-weight: 600; margin-bottom: 6px;">ECharts 未加载</div>
        <div style="color: var(--muted); font-size: 13px; line-height: 1.6;">
          请确认文件 <code>static/vendor/echarts/echarts.min.js</code> 已存在，且可被 Flask 正常访问。
        </div>`;
      c.prepend(box);
    }
    return true;
  }

  function clearCharts() {
    closeChartModal();
    for (const chart of app.chartInstances.values()) {
      try {
        chart.dispose();
      } catch (_) {
        // noop
      }
    }
    app.chartInstances.clear();
  }

  async function fetchAndRenderDetail(resourceId, metricKey, dom) {
    const key = `${resourceId}:${metricKey}`;
    if (app.chartInstances.has(key)) return;
    try {
      let chartData = app.chartDataByKey.get(key);
      if (!chartData) {
        let reqPromise = app.pendingDetailRequests.get(resourceId);
        if (!reqPromise) {
          reqPromise = api.requestJson(`/api/resources/${encodeURIComponent(resourceId)}`)
            .then((payload) => {
              if (payload?.resource?.prediction_pending) return payload;
              cacheResourceChartsFromPayload(payload, resourceId);
              return payload;
            })
            .finally(() => app.pendingDetailRequests.delete(resourceId));
          app.pendingDetailRequests.set(resourceId, reqPromise);
        }
        const payload = await reqPromise;
        if (payload?.resource?.prediction_pending) {
          dom.innerHTML = '<div style="padding:12px;color:#64748b;font-size:12px;">预测更新中...</div>';
          return;
        }
        chartData = app.chartDataByKey.get(key);
      }
      if (!chartData) return;
      const chart = echarts.init(dom, null, { renderer: app.ECHARTS_RENDERER });
      chart.setOption(buildChartOption(chartData, metricKey), { notMerge: true, lazyUpdate: false });
      app.chartInstances.set(key, chart);
      const row = dom.closest(".row");
      if (row) {
        for (const siblingDom of row.querySelectorAll(".chart")) {
          const sKey = `${resourceId}:${siblingDom.dataset.metricKey}`;
          if (sKey === key || app.chartInstances.has(sKey)) continue;
          if (siblingDom.closest(".img-wrap")?.classList.contains("mobile-hidden")) continue;
          const sData = app.chartDataByKey.get(sKey);
          if (!sData) continue;
          const sChart = echarts.init(siblingDom, null, { renderer: app.ECHARTS_RENDERER });
          sChart.setOption(buildChartOption(sData, siblingDom.dataset.metricKey), { notMerge: true, lazyUpdate: false });
          app.chartInstances.set(sKey, sChart);
          app.observer?.unobserve(siblingDom);
        }
      }
    } catch (_) {
      dom.innerHTML = '<div style="padding:12px;color:#ef4444;font-size:12px;">加载详情失败</div>';
    }
  }

  function bindLazyLoad() {
    if (app.observer) app.observer.disconnect();
    let pendingBatch = [];
    let batchTimer = null;
    app.observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) pendingBatch.push(entry);
      }
      if (batchTimer) clearTimeout(batchTimer);
      batchTimer = setTimeout(() => {
        const batch = pendingBatch;
        pendingBatch = [];
        for (const entry of batch) {
          const dom = entry.target;
          const resourceId = dom.dataset.resourceId;
          const metricKey = dom.dataset.metricKey;
          if (!resourceId || !metricKey) continue;
          fetchAndRenderDetail(resourceId, metricKey, dom);
          app.observer.unobserve(dom);
        }
      }, 50);
    }, { root: null, rootMargin: "120px", threshold: 0.05 });
    app.rowsRoot.querySelectorAll(".chart").forEach((dom) => app.observer.observe(dom));
  }

  function refreshRenderedCharts() {
    for (const [key, chart] of app.chartInstances.entries()) {
      const sep = key.lastIndexOf(":");
      const metricKey = sep >= 0 ? key.slice(sep + 1) : "";
      const chartData = app.chartDataByKey.get(key);
      if (!metricKey || !chartData) continue;
      chart.setOption(buildChartOption(chartData, metricKey), { notMerge: true, lazyUpdate: false });
    }
    if (app.modalChartInstance && app.modalChartContext) {
      const key = `${app.modalChartContext.resourceId}:${app.modalChartContext.metricKey}`;
      const chartData = app.chartDataByKey.get(key);
      if (chartData) {
        app.modalChartInstance.setOption(buildModalChartOption(chartData, app.modalChartContext.metricKey), { notMerge: true, lazyUpdate: false });
      }
    }
  }

  function toggleChartAuxiliary() {
    app.chartAuxiliaryVisible = !app.chartAuxiliaryVisible;
    refreshRenderedCharts();
  }

  window.ResourceCharts = {
    bindLazyLoad,
    clearCharts,
    closeChartModal,
    fetchAndRenderDetail,
    openChartModal,
    prefetchResourceDetails,
    showEchartsError,
    toggleChartAuxiliary,
  };
})();
