(function () {
  const app = window.ResourcePredictApp;
  const api = window.ResourceApi;
  const charts = window.ResourceCharts;

  const urgencyTooltipText = [
    "U = B(action) + C + P1 + 0.25×ΣP2 + W + M + ΔSpec",
    "B: 扩容35 / 缩容18",
    "C: high6 / medium3 / low1",
    "P: P95、峰值、均值、趋势、波动压力",
    "W: 多指标触发加分；M: 混合信号加分",
    "ΔSpec: 目标规格变化幅度",
  ].join("\n");
  const confidenceTooltipText = [
    "Conf = 0.65×max(Si) + 0.35×avg(Si) + W - M",
    "Si: 单指标信号质量(0-100)",
    "扩容Si: 超阈幅度 + 高负载持续率 + 趋势 - 孤立峰值惩罚",
    "缩容Si: 低负载余量 + 低负载持续率 + 稳定性 + 下降趋势",
    "等级: high≥72 / medium≥45 / low<45",
  ].join("\n");

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function infoTip(text) {
    return `<span class="metric-help" tabindex="0" aria-label="${escapeHtml(text)}" data-tooltip="${escapeHtml(text)}">i</span>`;
  }

  function normalizeDisplayIp(raw) {
    const s = String(raw || "").trim();
    if (!s || s === "-") return "-";
    const ipv4s = s.match(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g);
    if (ipv4s && ipv4s.length) return ipv4s[0];
    return s.split(/\s+/).filter(Boolean)[0] || "-";
  }

  function formatScaledTime(epochMs) {
    const ts = Number(epochMs);
    if (!Number.isFinite(ts) || ts <= 0) return "";
    const diffMs = Date.now() - ts;
    if (diffMs >= 0 && diffMs < 60 * 1000) return "刚刚";
    if (diffMs >= 0 && diffMs < 60 * 60 * 1000) return `${Math.max(1, Math.floor(diffMs / 60000))}分钟前`;
    if (diffMs >= 0 && diffMs < 24 * 60 * 60 * 1000) return `${Math.floor(diffMs / 3600000)}小时前`;
    return new Date(ts).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function scaledMarkerHtml(spec) {
    const label = formatScaledTime(spec?.last_scaled_at_epoch_ms);
    if (!label) return "";
    const title = new Date(Number(spec.last_scaled_at_epoch_ms)).toLocaleString("zh-CN", { hour12: false });
    return `<div class="scaled-marker" title="最近调配时间：${escapeHtml(title)}"><span class="scaled-dot"></span><span>已调配 · ${escapeHtml(label)}</span></div>`;
  }

  function emptySpecHtml() {
    return `
      <div class="spec-grid">
        <div class="spec-item spec-item-full"><span class="spec-label">集群</span><span class="spec-value" title="-">-</span></div>
        <div class="spec-item spec-item-full"><span class="spec-label">IP</span><span class="spec-value" title="-">-</span></div>
        <div class="spec-item spec-item-third"><span class="spec-label">CPU</span><span class="spec-value" title="- 核">- 核</span></div>
        <div class="spec-item spec-item-third"><span class="spec-label">内存</span><span class="spec-value" title="- GB">- GB</span></div>
        <div class="spec-item spec-item-third"><span class="spec-label">硬盘</span><span class="spec-value" title="- GB">- GB</span></div>
      </div>`;
  }

  function formatVmSpec(spec) {
    if (!spec || typeof spec !== "object") return emptySpecHtml();
    const cluster = escapeHtml(String(spec.cluster || "").trim() || "-");
    const ip = escapeHtml(normalizeDisplayIp(spec.ip));
    const cpuCores = Number.isFinite(Number(spec.cpu_cores)) ? `${Number(spec.cpu_cores)} 核` : "- 核";
    const memoryGb = Number.isFinite(Number(spec.memory_gb)) ? `${Number(spec.memory_gb)} GB` : "- GB";
    const diskGb = Number.isFinite(Number(spec.disk_gb)) ? `${Number(spec.disk_gb)} GB` : "- GB";
    return `
      ${scaledMarkerHtml(spec)}
      <div class="spec-grid">
        <div class="spec-item spec-item-full"><span class="spec-label">集群</span><span class="spec-value" title="${cluster}">${cluster}</span></div>
        <div class="spec-item spec-item-full"><span class="spec-label">IP</span><span class="spec-value" title="${ip}">${ip}</span></div>
        <div class="spec-item spec-item-third"><span class="spec-label">CPU</span><span class="spec-value" title="${escapeHtml(cpuCores)}">${escapeHtml(cpuCores)}</span></div>
        <div class="spec-item spec-item-third"><span class="spec-label">内存</span><span class="spec-value" title="${escapeHtml(memoryGb)}">${escapeHtml(memoryGb)}</span></div>
        <div class="spec-item spec-item-third"><span class="spec-label">硬盘</span><span class="spec-value" title="${escapeHtml(diskGb)}">${escapeHtml(diskGb)}</span></div>
      </div>`;
  }

  function formatTargetSpec(spec) {
    if (!spec || typeof spec !== "object") return "目标规格: -";
    const cpu = Number(spec.cpu_cores);
    const mem = Number(spec.memory_gb);
    const disk = Number(spec.disk_gb);
    if (!Number.isFinite(cpu) || !Number.isFinite(mem) || !Number.isFinite(disk)) return "目标规格: -";
    return `目标规格: ${cpu} 核 / ${mem} GB / ${disk} GB`;
  }

  async function refreshAdviceSummary() {
    const url = api.buildQuery("/api/resources/advice-summary", {
      q: app.state.mode === "search" ? app.state.q : "",
    });
    const payload = await api.requestJson(url);
    const actionCounts = payload.action_counts || {};
    app.sumOut.textContent = String(actionCounts.scale_out || 0);
    app.sumIn.textContent = String(actionCounts.scale_in || 0);
    app.sumHold.textContent = String(actionCounts.hold || 0);
    app.sumMixed.textContent = String(actionCounts.mixed || 0);
    app.sumTotal.textContent = String(payload.total || 0);
  }

  function renderRows(items) {
    charts.clearCharts();
    app.rowsRoot.innerHTML = "";
    const metricActionText = { scale_out: "扩容", scale_in: "缩容", hold: "保持" };
    const metricActionArrow = { scale_out: "↑", scale_in: "↓", hold: "" };
    for (const item of items) {
      const node = app.rowTemplate.content.firstElementChild.cloneNode(true);
      const resourceId = String(item.resource_id || "");
      const urgencyScoreText = `紧迫度 ${Number(item.urgency_score || 0).toFixed(3)}`;
      node.dataset.resourceId = resourceId;
      const resourceIdEl = node.querySelector('[data-role="resource-id"]');
      resourceIdEl.textContent = resourceId;
      resourceIdEl.title = resourceId;
      node.querySelector('[data-role="resource-score"]').innerHTML =
        `<span class="chip-text" title="${escapeHtml(urgencyScoreText)}">${escapeHtml(urgencyScoreText)}</span>${infoTip(urgencyTooltipText)}`;

      const advice = item.scaling_advice || {};
      const vmSpec = item.vm_spec || {};
      const lastScaledAt = Number(vmSpec.last_scaled_at_epoch_ms || 0);
      if (Number.isFinite(lastScaledAt) && lastScaledAt > 0 && Date.now() - lastScaledAt < 24 * 60 * 60 * 1000) {
        node.classList.add("is-recently-scaled");
      }
      const action = String(advice.action || "hold");
      const targetSpecText = formatTargetSpec(advice.target_vm_spec || {});
      const confidence = String(advice.confidence || "medium");
      const hasMixed = !!advice.has_mixed_signals;
      const metricActions = advice.metric_actions || {};
      const actionClass = hasMixed ? "is-mixed" : (
        action === "scale_out" ? "is-scale-out" : (action === "scale_in" ? "is-scale-in" : "is-hold")
      );
      node.classList.add(actionClass);

      const pillsHtml = hasMixed
        ? `<span class="advice-pill advice-mixed" title="混合调整">混合调整</span>`
        : (() => {
            const adviceLabel = action === "scale_out" ? "建议扩容" : (action === "scale_in" ? "建议缩容" : "建议保持");
            const adviceClass = action === "scale_out" ? "advice-out" : (action === "scale_in" ? "advice-in" : "advice-hold");
            return `<span class="advice-pill ${adviceClass}" title="${escapeHtml(adviceLabel)}">${adviceLabel}</span>`;
          })();

      const metricPillsHtml = ["cpu", "memory", "disk"].map((metricKey) => {
        const metricAction = String(metricActions[metricKey] || "hold");
        const cls = metricAction === "scale_out" ? "advice-out" : (metricAction === "scale_in" ? "advice-in" : "advice-hold");
        const actionText = metricActionText[metricAction] || "保持";
        const arrow = metricActionArrow[metricAction] || "";
        const label = `${app.metricTitleMap[metricKey]} ${actionText}${arrow}`;
        const arrowHtml = arrow ? `<span class="metric-arrow" aria-hidden="true">${escapeHtml(arrow)}</span>` : "";
        return `<span class="metric-pill ${cls}" title="${escapeHtml(label)}"><span class="metric-name">${escapeHtml(app.metricTitleMap[metricKey])}</span><span class="metric-action">${escapeHtml(actionText)}</span>${arrowHtml}</span>`;
      }).join("");

      const defaultMetricKey = ["cpu", "memory", "disk"].find((metricKey) => String(metricActions[metricKey] || "hold") !== "hold") || "cpu";
      const mobileTabsHtml = ["cpu", "memory", "disk"].map((metricKey) => {
        const active = metricKey === defaultMetricKey ? " active" : "";
        return `<button type="button" class="mobile-metric-tab${active}" data-mobile-metric="${metricKey}">${app.metricTitleMap[metricKey]}</button>`;
      }).join("");

      node.querySelector('[data-role="resource-advice"]').innerHTML =
        `<div class="advice-head">` +
        pillsHtml +
        `<span class="advice-confidence"><span class="chip-text" title="置信度 ${escapeHtml(confidence)}">置信度 ${escapeHtml(confidence)}</span>${infoTip(confidenceTooltipText)}</span>` +
        `</div>` +
        `<div class="metric-pills">${metricPillsHtml}</div>` +
        `<div class="advice-target"><span class="target-pill ${action === "scale_out" ? "advice-out" : (action === "scale_in" ? "advice-in" : "advice-hold")}" title="${escapeHtml(targetSpecText)}">${escapeHtml(targetSpecText)}</span></div>` +
        window.ScalingUI.buildControls(resourceId, action, confidence, hasMixed);
      node.querySelector('[data-role="resource-spec"]').innerHTML = formatVmSpec(vmSpec);
      node.querySelector('[data-role="mobile-metric-tabs"]').innerHTML = mobileTabsHtml;

      for (const metricKey of ["cpu", "memory", "disk"]) {
        const c = node.querySelector(`[data-metric="${metricKey}"]`);
        c.dataset.resourceId = resourceId;
        c.dataset.metricKey = metricKey;
        if (metricKey !== defaultMetricKey) {
          c.closest(".img-wrap")?.classList.add("mobile-hidden");
        }
      }
      app.rowsRoot.appendChild(node);
    }
  }

  window.ResourceList = { refreshAdviceSummary, renderRows };
})();
