(function () {
  const app = window.ResourcePredictApp;

  const ACTION_LABELS = {
    scale_out: "扩容",
    scale_in: "缩容",
    scale_out_candidate: "扩容候选",
    scale_in_candidate: "缩容候选",
    hold: "保持",
    insufficient_data: "数据不足",
    mixed: "混合信号",
  };

  const CONFIDENCE_LABELS = {
    high: "High",
    medium: "Medium",
    low: "Low",
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function resourceTypeOf(item) {
    const raw = String(item?.resource_type || "").toLowerCase().replaceAll("-", "_");
    if (raw === "k8s_workload" || raw === "k8s_controller" || raw === "workload" || raw === "controller") return "k8s_workload";
    if (raw === "k8s_pod" || raw === "pod") return "k8s_pod";
    return "openstack_vm";
  }

  function isK8s(item) {
    return resourceTypeOf(item) === "k8s_workload" || resourceTypeOf(item) === "k8s_pod";
  }

  function typeLabel(item) {
    if (resourceTypeOf(item) === "k8s_workload") return "Workload";
    if (resourceTypeOf(item) === "k8s_pod") return "Pod";
    return "VM";
  }

  function actionOf(item) {
    const advice = item?.scaling_advice || {};
    if (advice.has_mixed_signals) return "mixed";
    return String(advice.action || "hold").toLowerCase();
  }

  function actionLabel(action) {
    return ACTION_LABELS[action] || ACTION_LABELS.hold;
  }

  function confidenceOf(item) {
    return String(item?.scaling_advice?.confidence || "medium").toLowerCase();
  }

  function metricKeysFor(item) {
    const type = resourceTypeOf(item);
    return app.viewMetricMap[type] || app.viewMetricMap.openstack_vm;
  }

  function formatPct(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    return `${(n * 100).toFixed(0)}%`;
  }

  function formatNumber(value, digits = 1) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    return n.toFixed(digits).replace(/\.0+$/, "");
  }

  function triggerMetric(item) {
    const metricActions = item?.scaling_advice?.metric_actions || {};
    return metricKeysFor(item).find((key) => String(metricActions[key] || "hold") !== "hold") || metricKeysFor(item)[0];
  }

  function metricSummary(item) {
    const stats = item?.scaling_advice?.stats || {};
    const metricActions = item?.scaling_advice?.metric_actions || {};
    return metricKeysFor(item).map((key) => {
      const stat = stats[key] || {};
      const action = String(metricActions[key] || "hold");
      const p95 = stat.p95 !== undefined ? `P95 ${formatPct(stat.p95)}` : "";
      return `<span class="metric-pill is-${escapeHtml(action)}">${escapeHtml(app.metricTitleMap[key])} ${escapeHtml(actionLabel(action))}${p95 ? ` · ${escapeHtml(p95)}` : ""}</span>`;
    }).join("");
  }

  function targetSpecText(item) {
    const advice = item?.scaling_advice || {};
    if (isK8s(item)) return advice.analysis_only ? "仅分析，不执行 K8S 调配" : "K8S 建议";
    const target = advice.target_spec || {};
    const cpu = formatNumber(target.cpu_cores, 0);
    const memory = formatNumber(target.memory_gb, 0);
    const disk = formatNumber(target.disk_gb, 0);
    if (cpu === "-" && memory === "-" && disk === "-") return "目标规格待确认";
    return `目标规格 ${cpu}C / ${memory}GB / ${disk}GB`;
  }

  function subtitleFor(item) {
    const spec = item?.spec || {};
    if (isK8s(item)) {
      return [
        spec.cluster,
        spec.namespace,
        [spec.workload_kind || spec.owner_kind, spec.workload_name || spec.owner_name].filter(Boolean).join("/"),
        spec.replicas_observed ? `${formatNumber(spec.replicas_observed, 0)} 副本` : "",
      ].filter(Boolean).join(" / ") || "-";
    }
    return [
      spec.cluster,
      spec.ip,
      `${formatNumber(spec.cpu_cores, 0)}C`,
      `${formatNumber(spec.memory_gb, 0)}GB`,
      `${formatNumber(spec.disk_gb, 0)}GB`,
    ].filter((x) => x && !String(x).startsWith("-")).join(" / ") || "-";
  }

  function applyClientFilters(items, options = {}) {
    const confidence = app.state.confidenceFilter;
    const actionFilter = options.ignoreAction ? "" : app.state.actionFilter;
    let rows = items || [];
    if (confidence) rows = rows.filter((item) => confidenceOf(item) === confidence);
    if (actionFilter) {
      rows = rows.filter((item) => {
        const action = actionOf(item);
        if (actionFilter === "scale_out") return action === "scale_out" || action === "scale_out_candidate";
        if (actionFilter === "scale_in") return action === "scale_in" || action === "scale_in_candidate";
        return action === actionFilter;
      });
    }
    return rows;
  }

  function currentPageItems() {
    const start = (app.state.page - 1) * app.state.pageSize;
    return app.state.visibleItems.slice(start, start + app.state.pageSize);
  }

  function updatePager() {
    const totalPages = Math.max(1, Math.ceil(app.state.visibleItems.length / app.state.pageSize));
    app.state.page = Math.min(app.state.page, totalPages);
    app.els.prevPageBtn.disabled = app.state.page <= 1;
    app.els.nextPageBtn.disabled = app.state.page >= totalPages;
    app.els.pagerText.textContent = `第 ${app.state.page} / ${totalPages} 页`;
  }

  function updateSummary() {
    const summaryItems = applyClientFilters(app.state.loadedItems, { ignoreAction: true });
    const counts = { scale_out: 0, scale_in: 0, mixed: 0, hold: 0 };
    for (const item of summaryItems) {
      const action = actionOf(item);
      if (action === "scale_out" || action === "scale_out_candidate") counts.scale_out += 1;
      else if (action === "scale_in" || action === "scale_in_candidate") counts.scale_in += 1;
      else if (action === "mixed") counts.mixed += 1;
      else counts.hold += 1;
    }
    app.els.sumOut.textContent = String(counts.scale_out);
    app.els.sumIn.textContent = String(counts.scale_in);
    app.els.sumMixed.textContent = String(counts.mixed);
    app.els.sumHold.textContent = String(counts.hold);
    app.els.sumTotal.textContent = String(summaryItems.length);
    app.els.summaryText.textContent = `匹配 ${app.state.visibleItems.length} 个资源，当前显示 ${currentPageItems().length} 个`;
    app.els.summaryItems.forEach((el) => {
      el.classList.toggle("active", (el.dataset.summaryAction || "") === app.state.actionFilter);
    });
    renderOverview(counts, summaryItems);
  }

  function renderOverview(counts, summaryItems) {
    if (!app.els.overviewGrid) return;
    const byType = summaryItems.reduce((acc, item) => {
      acc[typeLabel(item)] = (acc[typeLabel(item)] || 0) + 1;
      return acc;
    }, {});
    app.els.overviewGrid.innerHTML = [
      ["当前范围", summaryItems.length],
      ["VM", byType.VM || 0],
      ["Workload", byType.Workload || 0],
      ["Pod", byType.Pod || 0],
      ["需扩容", counts.scale_out],
      ["需缩容", counts.scale_in],
      ["混合信号", counts.mixed],
    ].map(([k, v]) => `<div class="overview-card"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join("");
  }

  function renderRows() {
    const root = app.els.rowsRoot;
    const items = currentPageItems();
    root.innerHTML = "";
    if (!items.length) {
      root.innerHTML = `<div class="empty-list">没有匹配的资源。调整筛选条件后再试。</div>`;
      updatePager();
      updateSummary();
      return;
    }
    for (const item of items) {
      const action = actionOf(item);
      const confidence = confidenceOf(item);
      const selected = String(item.resource_id || "") === app.state.selectedResourceId;
      const node = document.createElement("button");
      node.type = "button";
      node.className = `risk-row is-${action}${selected ? " active" : ""}`;
      node.dataset.resourceId = String(item.resource_id || "");
      node.innerHTML = `
        <span class="row-accent"></span>
        <span class="row-main">
          <span class="row-title">
            <strong title="${escapeHtml(item.resource_id)}">${escapeHtml(item.resource_id)}</strong>
            <span class="resource-type-badge">${typeLabel(item)}</span>
            <span class="action-chip is-${escapeHtml(action)}">${escapeHtml(actionLabel(action))}</span>
          </span>
          <span class="row-subtitle">${escapeHtml(subtitleFor(item))}</span>
          <span class="row-metrics">${metricSummary(item)}</span>
        </span>
        <span class="row-side">
          <span class="score-block" title="紧急度分数：根据建议动作、置信度、预测压力和目标规格变化计算，用于队列排序">
            <span class="score-label">紧急度</span>
            <span class="score">${formatNumber(item.urgency_score || 0, 1)}</span>
          </span>
          <span class="confidence-chip is-${escapeHtml(confidence)}">${escapeHtml(CONFIDENCE_LABELS[confidence] || confidence)}</span>
          <span class="target-text" title="${escapeHtml(targetSpecText(item))}">${escapeHtml(targetSpecText(item))}</span>
        </span>`;
      root.appendChild(node);
    }
    updatePager();
    updateSummary();
  }

  function setItems(items) {
    app.state.loadedItems = items || [];
    app.state.visibleItems = applyClientFilters(app.state.loadedItems);
    app.state.total = app.state.visibleItems.length;
    if (app.state.page < 1) app.state.page = 1;
    renderRows();
    if (!app.state.selectedResourceId && app.state.visibleItems.length) {
      if (window.matchMedia("(max-width: 1180px)").matches) {
        hideDetail();
      } else {
        selectResource(app.state.visibleItems[0].resource_id);
      }
    } else if (app.state.selectedResourceId && !app.state.visibleItems.some((x) => x.resource_id === app.state.selectedResourceId)) {
      if (window.matchMedia("(max-width: 1180px)").matches) {
        app.state.selectedResourceId = "";
        hideDetail();
      } else {
        selectResource(app.state.visibleItems[0]?.resource_id || "");
      }
    }
  }

  function syncFilterButtons() {
    document.querySelectorAll("[data-filter-group]").forEach((group) => {
      const filter = group.dataset.filterGroup;
      const current = filter === "resource-type"
        ? app.state.resourceTypeFilter
        : filter === "confidence"
          ? app.state.confidenceFilter
          : app.state.actionFilter;
      group.querySelectorAll("button").forEach((btn) => {
        btn.classList.toggle("active", (btn.dataset.value || "") === current);
      });
    });
  }

  async function selectResource(resourceId, metricKey) {
    app.state.selectedResourceId = String(resourceId || "");
    app.els.detailPanel.dataset.resourceId = app.state.selectedResourceId;
    renderRows();
    if (!app.state.selectedResourceId) {
      hideDetail();
      return;
    }
    await window.ResourceCharts.renderDetail(app.state.selectedResourceId, metricKey);
    window.dispatchEvent(new CustomEvent("resource-selected", {
      detail: { resourceId: app.state.selectedResourceId },
    }));
  }

  function hideDetail() {
    app.els.detailEmpty.hidden = false;
    app.els.detailContent.hidden = true;
    app.els.detailPanel.classList.remove("is-open");
  }

  window.ResourceList = {
    ACTION_LABELS,
    CONFIDENCE_LABELS,
    actionLabel,
    actionOf,
    applyClientFilters,
    confidenceOf,
    escapeHtml,
    formatNumber,
    formatPct,
    isPod: (item) => resourceTypeOf(item) === "k8s_pod",
    isK8s,
    metricKeysFor,
    renderRows,
    resourceTypeOf,
    selectResource,
    setItems,
    subtitleFor,
    syncFilterButtons,
    targetSpecText,
    triggerMetric,
    typeLabel,
  };
})();
