(function () {
  const app = window.ResourcePredictApp;
  const api = window.ResourceApi;
  const list = window.ResourceList;
  const CHART_TIME_ZONE = "UTC";

  function toPairs(xMs, y) {
    const res = [];
    for (let i = 0; i < xMs.length; i++) {
      let ts = xMs[i];
      if (typeof ts === "number" && ts < 1e12) ts *= 1000;
      res.push([ts, y[i]]);
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

  function buildTimeAxisConfig(xTrain, xTest, xPredFuture) {
    const bucket = [];
    for (const arr of [xTrain, xTest, xPredFuture]) {
      if (!Array.isArray(arr)) continue;
      for (const t of arr) {
        const ms = normalizeTsMs(t);
        if (Number.isFinite(ms)) bucket.push(ms);
      }
    }
    if (!bucket.length) return { spanMs: 0 };
    bucket.sort((a, b) => a - b);
    const spanMs = bucket[bucket.length - 1] - bucket[0];
    const pad = Math.max(spanMs * 0.02, 60 * 1000);
    return { min: bucket[0] - pad, max: bucket[bucket.length - 1] + pad, spanMs };
  }

  function metricThresholds(metricKey) {
    return {
      upper: { value: 0.8, label: `${app.metricTitleMap[metricKey] || metricKey} 扩容阈值` },
      lower: { value: 0.2, label: `${app.metricTitleMap[metricKey] || metricKey} 缩容阈值` },
    };
  }

  function buildChartOption(chartData, metricKey) {
    const bestMethod = chartData.best_method;
    const xTrain = Array.isArray(chartData.x_train_ms) ? chartData.x_train_ms : [];
    const yTrain = Array.isArray(chartData.y_train) ? chartData.y_train : [];
    const xTest = Array.isArray(chartData.x_test_ms) ? chartData.x_test_ms : [];
    const yTest = Array.isArray(chartData.y_test) ? chartData.y_test : [];
    const xPredFuture = Array.isArray(chartData.x_pred_ms) ? chartData.x_pred_ms : [];
    const anchorTs = xTrain.length ? xTrain[xTrain.length - 1] : null;
    const anchorVal = yTrain.length ? yTrain[yTrain.length - 1] : null;
    const series = [{
      name: "历史",
      type: "line",
      data: toPairs(xTrain, yTrain),
      showSymbol: false,
      lineStyle: { color: "#2563eb", width: 2.4 },
      itemStyle: { color: "#2563eb" },
      z: 2,
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
            { yAxis: thresholds.upper.value, name: thresholds.upper.label, lineStyle: { color: "rgba(220,38,38,.62)", type: "dashed", width: 1.2 } },
            { yAxis: thresholds.lower.value, name: thresholds.lower.label, lineStyle: { color: "rgba(5,150,105,.62)", type: "dashed", width: 1.2 } },
          ],
        },
        markArea: futureStart != null && futureEnd != null ? {
          silent: true,
          itemStyle: { color: "rgba(217,119,6,.09)" },
          data: [[{ xAxis: futureStart }, { xAxis: futureEnd }]],
        } : undefined,
        z: 0,
      });
    }

    series.push({
      name: "测试",
      type: "line",
      data: toPairs(anchorTs == null ? xTest : [anchorTs].concat(xTest), anchorVal == null ? yTest : [anchorVal].concat(yTest)),
      showSymbol: false,
      lineStyle: { color: "#dc2626", width: 2.4 },
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
      legendData.push(label);
      series.push({
        name: label,
        type: "line",
        data: toPairs(predX, predY),
        showSymbol: false,
        lineStyle: {
          type: "dashed",
          color: app.colorMap[m] || "#64748b",
          width: m === bestMethod ? 3 : 2,
          opacity: m === bestMethod ? 1 : 0.72,
        },
        itemStyle: { color: app.colorMap[m] || "#64748b" },
        z: 4,
      });
    }

    const bestRmse = chartData.metrics?.[bestMethod]?.rmse;
    const timeAxis = buildTimeAxisConfig(xTrain, xTest, xPredFuture);
    return {
      backgroundColor: "transparent",
      animation: false,
      title: {
        text: `${app.metricTitleMap[metricKey] || metricKey} 预测${bestMethod ? ` · 最优 ${app.labelMap[bestMethod] || bestMethod}` : ""}${bestRmse !== undefined ? ` · RMSE ${bestRmse.toFixed(3)}` : ""}`,
        left: "center",
        top: 6,
        textStyle: { color: "#0f172a", fontSize: 13, fontWeight: 800 },
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
              ? `${p.marker}${p.seriesName}: ${(num * 100).toFixed(2)}%<br/>`
              : `${p.marker}${p.seriesName}: ${raw}<br/>`;
          }
          return html;
        },
      },
      legend: { top: 34, left: 8, data: legendData, itemWidth: 12, itemHeight: 7, textStyle: { color: "#475569", fontSize: 10 } },
      grid: { left: 48, right: 18, top: 70, bottom: timeAxis.spanMs > 14 * 86400000 ? 48 : 40 },
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
        axisLabel: { color: "#64748b", formatter: (v) => `${(Number(v) * 100).toFixed(0)}%` },
        splitLine: { lineStyle: { color: "rgba(15,23,42,.08)" } },
      },
      series,
    };
  }

  function cacheResourceChartsFromPayload(payload, fallbackResourceId) {
    const resource = payload?.resource;
    if (!resource || resource.prediction_pending) return;
    const rid = String(resource.resource_id || fallbackResourceId || "");
    if (!rid) return;
    app.resourcePayloadCache.set(rid, payload);
    const charts = resource.charts || {};
    const metricKeys = list.metricKeysFor(resource);
    for (const mk of metricKeys) {
      if (charts[mk]) app.chartDataByKey.set(`${rid}:${mk}`, charts[mk]);
    }
  }

  async function ensureResource(resourceId) {
    const cached = app.resourcePayloadCache.get(resourceId);
    if (cached?.resource) return cached.resource;
    let req = app.pendingDetailRequests.get(resourceId);
    if (!req) {
      req = api.requestJson(`/api/resources/${encodeURIComponent(resourceId)}`)
        .then((payload) => {
          cacheResourceChartsFromPayload(payload, resourceId);
          return payload;
        })
        .finally(() => app.pendingDetailRequests.delete(resourceId));
      app.pendingDetailRequests.set(resourceId, req);
    }
    const payload = await req;
    return payload.resource;
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

  function renderModalMetricTabs(resource, activeMetric) {
    if (!app.els.chartModalMetricTabs) return;
    const metricKeys = list.metricKeysFor(resource);
    app.els.chartModalMetricTabs.innerHTML = metricKeys.map((key) => (
      `<button type="button" class="${key === activeMetric ? "active" : ""}" data-modal-metric-key="${list.escapeHtml(key)}">${list.escapeHtml(app.metricTitleMap[key])}</button>`
    )).join("");
  }

  function renderSpec(resource) {
    const spec = resource?.spec || {};
    const formatMaybe = (value, unit, digits = 2) => {
      if (value === undefined || value === null || value === "") return "-";
      const text = list.formatNumber(value, digits);
      return text === "-" ? "-" : `${text} ${unit}`;
    };
    const entries = list.isK8s(resource)
      ? [
          ["集群", spec.cluster],
          ["Namespace", spec.namespace],
          ["Workload", [spec.workload_kind || spec.owner_kind, spec.workload_name || spec.owner_name].filter(Boolean).join("/")],
          ["副本数", spec.replicas_observed],
          ["容器", Array.isArray(spec.containers_observed) ? spec.containers_observed.join(", ") : spec.container],
          ["节点", Array.isArray(spec.nodes) ? spec.nodes.join(", ") : spec.node],
          ["CPU Request", formatMaybe(spec.cpu_request_cores, "C")],
          ["CPU Limit", formatMaybe(spec.cpu_limit_cores, "C")],
          ["内存 Request", formatMaybe(spec.memory_request_gb, "GB")],
          ["内存 Limit", formatMaybe(spec.memory_limit_gb, "GB")],
          ["CPU 基准", spec.cpu_metric_mode],
          ["内存基准", spec.memory_metric_mode],
        ]
      : [
          ["集群", spec.cluster],
          ["IP", spec.ip],
          ["CPU", `${list.formatNumber(spec.cpu_cores, 0)} 核`],
          ["内存", `${list.formatNumber(spec.memory_gb, 0)} GB`],
          ["磁盘", `${list.formatNumber(spec.disk_gb, 0)} GB`],
        ];
    app.els.detailSpec.innerHTML = entries
      .filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "")
      .map(([label, value]) => `<div class="spec-item"><span>${list.escapeHtml(label)}</span><strong title="${list.escapeHtml(value)}">${list.escapeHtml(value)}</strong></div>`)
      .join("");
  }

  function renderAdvice(resource) {
    const advice = resource?.scaling_advice || {};
    const action = list.actionOf(resource);
    const confidence = list.confidenceOf(resource);
    const stats = advice.stats || {};
    const resourceLabel = list.isK8s(resource) ? "K8S Workload" : "VM";
    const actionText = list.actionLabel(action);
    const decisionResource = `${resourceLabel} ${resource.resource_id || ""}`;
    app.els.detailConfidence.innerHTML = `置信度 ${list.escapeHtml(list.CONFIDENCE_LABELS[confidence] || confidence)}${advice.confidence_score ? ` · ${list.formatNumber(advice.confidence_score, 1)}分` : ""} ${list.infoTooltip(list.CONFIDENCE_HELP, "置信度计算说明")}`;
    app.els.detailConfidence.className = `confidence-chip is-${confidence}`;
    app.els.detailAdvice.innerHTML = `
      <div class="decision-summary is-${list.escapeHtml(action)}">
        <div class="decision-row">
          <span class="decision-label">建议动作</span>
          <span class="decision-action">${list.escapeHtml(actionText)}</span>
        </div>
        <div class="decision-row">
          <span class="decision-label">资源对象</span>
          <strong>${list.escapeHtml(decisionResource)}</strong>
        </div>
        <div class="decision-row">
          <span class="decision-label">目标结果</span>
          <small>${list.escapeHtml(list.targetSpecText(resource))}</small>
        </div>
      </div>
      <div class="reason-grid">
        ${list.metricKeysFor(resource).map((key) => {
          const st = stats[key] || {};
          const mAction = String(advice.metric_actions?.[key] || "hold");
          return `<div class="reason-item">
            <span class="reason-metric">${list.escapeHtml(app.metricTitleMap[key])}</span>
            <strong class="reason-action">${list.escapeHtml(list.actionLabel(mAction))}</strong>
            <small class="reason-stats">平均 ${list.formatPct(st.avg)} · P95 ${list.formatPct(st.p95)} · 峰值 ${list.formatPct(st.peak)}</small>
          </div>`;
        }).join("")}
      </div>`;

    app.els.detailActions.innerHTML = window.ScalingUI.buildControls(
      resource.resource_id,
      action,
      confidence,
      !!advice.has_mixed_signals,
      { analysisOnly: Boolean(advice.analysis_only), resourceType: list.resourceTypeOf(resource) }
    );
  }

  function renderMetricTabs(resource, activeMetric) {
    const metricKeys = list.metricKeysFor(resource);
    app.els.metricTabs.innerHTML = metricKeys.map((key) => (
      `<button type="button" class="${key === activeMetric ? "active" : ""}" data-metric-key="${list.escapeHtml(key)}">${list.escapeHtml(app.metricTitleMap[key])}</button>`
    )).join("");
  }

  async function renderChart(resourceId, metricKey) {
    disposeDetailChart();
    app.els.detailChart.innerHTML = "";
    const key = `${resourceId}:${metricKey}`;
    const data = app.chartDataByKey.get(key);
    if (!data) {
      app.els.detailChart.innerHTML = `<div class="chart-state">暂无 ${list.escapeHtml(app.metricTitleMap[metricKey] || metricKey)} 图表数据</div>`;
      return;
    }
    if (typeof echarts === "undefined") {
      app.els.detailChart.innerHTML = `<div class="chart-state is-error">ECharts 未加载</div>`;
      return;
    }
    app.detailChartInstance = echarts.init(app.els.detailChart, null, { renderer: app.ECHARTS_RENDERER });
    app.detailChartInstance.setOption(buildChartOption(data, metricKey), { notMerge: true, lazyUpdate: false });
    requestAnimationFrame(() => app.detailChartInstance?.resize());
  }

  async function openChartModal(metricKey) {
    if (!app.state.selectedResourceId || !app.state.selectedMetricKey) return;
    const resource = await ensureResource(app.state.selectedResourceId);
    const metricKeys = list.metricKeysFor(resource);
    let activeMetric = metricKey || app.state.selectedMetricKey || list.triggerMetric(resource);
    if (!metricKeys.includes(activeMetric)) activeMetric = list.triggerMetric(resource);
    app.state.selectedMetricKey = activeMetric;
    const key = `${app.state.selectedResourceId}:${activeMetric}`;
    const data = app.chartDataByKey.get(key);
    if (!data || typeof echarts === "undefined" || !app.els.chartModal || !app.els.chartModalChart) return;
    disposeModalChart();
    app.els.chartModal.hidden = false;
    const metricName = app.metricTitleMap[activeMetric] || activeMetric;
    app.els.chartModalTitle.textContent = `${metricName} 指标预测`;
    app.els.chartModalSubtitle.textContent = app.state.selectedResourceId;
    renderMetricTabs(resource, activeMetric);
    renderModalMetricTabs(resource, activeMetric);
    app.els.chartModalChart.innerHTML = "";
    app.modalChartInstance = echarts.init(app.els.chartModalChart, null, { renderer: app.ECHARTS_RENDERER });
    app.modalChartInstance.setOption(buildChartOption(data, activeMetric), { notMerge: true, lazyUpdate: false });
    renderChart(app.state.selectedResourceId, activeMetric);
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
    app.els.detailTitle.textContent = resourceId;
    app.els.detailSubtitle.textContent = "正在加载详情...";
    app.els.detailAdvice.innerHTML = `<div class="chart-state">正在加载建议和图表...</div>`;
    try {
      const resource = await ensureResource(resourceId);
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
      app.els.detailTitle.textContent = resource.resource_id || resourceId;
      app.els.detailSubtitle.textContent = list.subtitleFor(resource);
      renderAdvice(resource);
      renderSpec(resource);
      renderMetricTabs(resource, activeMetric);
      await renderChart(resource.resource_id || resourceId, activeMetric);
    } catch (e) {
      app.els.detailSubtitle.textContent = "加载失败";
      app.els.detailAdvice.innerHTML = `<div class="chart-state is-error">详情加载失败：${list.escapeHtml(e.message || e)}</div>`;
    }
  }

  function toggleChartAuxiliary() {
    app.chartAuxiliaryVisible = !app.chartAuxiliaryVisible;
    app.els.chartGuideBtn.textContent = app.chartAuxiliaryVisible ? "辅助线：开" : "辅助线：关";
    app.els.chartGuideBtn.setAttribute("aria-pressed", app.chartAuxiliaryVisible ? "true" : "false");
    if (app.state.selectedResourceId && app.state.selectedMetricKey) {
      renderChart(app.state.selectedResourceId, app.state.selectedMetricKey);
      if (app.els.chartModal && !app.els.chartModal.hidden) openChartModal();
    }
  }

  window.addEventListener("resize", () => {
    app.detailChartInstance?.resize();
    app.modalChartInstance?.resize();
  });

  app.els.chartModal?.addEventListener("click", (event) => {
    if (event.target.closest("[data-chart-modal-dismiss]")) closeChartModal();
  });

  app.els.chartModalMetricTabs?.addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-modal-metric-key]");
    if (!btn) return;
    openChartModal(btn.dataset.modalMetricKey || app.state.selectedMetricKey);
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && app.els.chartModal && !app.els.chartModal.hidden) closeChartModal();
  });

  window.ResourceCharts = {
    cacheResourceChartsFromPayload,
    closeChartModal,
    disposeDetailChart,
    openChartModal,
    renderDetail,
    toggleChartAuxiliary,
  };
})();
