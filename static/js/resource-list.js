(function () {
  const app = window.ResourcePredictApp;

  const ACTION_LABELS = {
    scale_out: "扩容",
    scale_in: "缩容",
    scale_out_candidate: "扩容",
    scale_in_candidate: "缩容",
    hold: "保持",
    insufficient_data: "数据不足",
    mixed: "混合信号",
  };

  const CONFIDENCE_LABELS = {
    high: "High",
    medium: "Medium",
    low: "Low",
  };

  const URGENCY_HELP = [
    "紧急度用于风险队列排序，分数越高越优先处理。",
    "计算口径：基础动作分 + 置信度加成 + 风险分贡献 + 指标压力/空闲信号 + 多指标加成 + 混合信号加成 + 目标规格变化分。",
    "风险分最多贡献 20 分；混合信号会额外加 4 分；目标规格变化越大，排序越靠前。",
  ].join("\n");

  const CONFIDENCE_HELP = [
    "置信度表示当前扩缩容信号的可靠程度。",
    "VM：综合 P95、峰值、平均值、持续高/低负载比例、趋势和尖峰惩罚；多指标一致会加分，混合信号会扣 8 分。",
    "K8S：还会考虑数据质量、是否缺少 request/limit 基线，以及是否能生成目标策略。",
  ].join("\n");

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function infoTooltip(text, label = "说明") {
    return `<span class="info-tooltip" role="img" aria-label="${escapeHtml(label)}">i<span class="tooltip-bubble" aria-hidden="true">${escapeHtml(text)}</span></span>`;
  }

  function resourceTypeOf(item) {
    const raw = String(item?.resource_type || "").toLowerCase().replaceAll("-", "_");
    if (raw === "openstack_vm" || raw === "openstack" || raw === "vm") return "openstack_vm";
    return "k8s_workload";
  }

  function isK8s(item) {
    return resourceTypeOf(item) === "k8s_workload";
  }

  function typeLabel(item) {
    if (resourceTypeOf(item) === "k8s_workload") return "Workload";
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

  /**
   * 根据资源类型和指标 key 判断图表 Y 轴显示单位。
   * K8S Workload 在缺少 request/limit 时，后端使用绝对值模式
   * （cpu_usage_cores / memory_working_set_gb），前端需要对应显示
   * Cores / GiB 而非百分比。
   */
  function resolveDisplayUnit(resource, metricKey) {
    if (!resource) return "percent";
    if (!isK8s(resource)) return "percent";
    const spec = resource.spec || {};
    if (metricKey === "cpu_limit" || metricKey === "cpu_request") {
      const mode = String(spec[`${metricKey}_metric_mode`] || "");
      if (mode.includes("cpu_usage_cores") || mode === "raw") return "cores";
    }
    if (metricKey === "memory_limit" || metricKey === "memory_request") {
      const mode = String(spec[`${metricKey}_metric_mode`] || "");
      if (mode.includes("memory_working_set_gb") || mode === "raw") return "gib";
    }
    return "percent";
  }

  /**
   * 按 displayUnit 格式化统计值（用于 tooltip、Y 轴、建议面板等）。
   */
  function formatStatValue(value, displayUnit) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    if (displayUnit === "cores") return `${n.toFixed(2)} C`;
    if (displayUnit === "gib") return formatMemoryGiB(n);
    return `${(n * 100).toFixed(1)}%`;
  }

  function formatMemoryGiB(value, digits = 2) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    if (Math.abs(n) < 1) return `${formatNumber(n * 1024, 0)} MiB`;
    return `${formatNumber(n, digits)} GiB`;
  }

  function formatNumber(value, digits = 1) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    return n.toFixed(digits).replace(/\.0+$/, "");
  }

  function baseMetricKey(metricKey) {
    if (String(metricKey).startsWith("cpu_")) return "cpu";
    if (String(metricKey).startsWith("memory_")) return "memory";
    return metricKey;
  }

  function metricStatsFor(item, metricKey) {
    const advice = item?.scaling_advice || {};
    const stats = advice.stats || {};
    const signalStats = advice.signal_stats || {};
    const baseKey = baseMetricKey(metricKey);
    return signalStats[metricKey] || stats[metricKey] || stats[baseKey] || {};
  }

  function metricActionFor(item, metricKey) {
    const metricActions = item?.scaling_advice?.metric_actions || {};
    const baseKey = baseMetricKey(metricKey);
    return String(metricActions[metricKey] || metricActions[baseKey] || "hold");
  }

  function metricActionLabel(item, metricKey, action) {
    return actionLabel(action);
  }

  function replicaTargetSummary(item) {
    const target = item?.scaling_advice?.target_spec || {};
    const targetReplicas = Number(target.replicas);
    if (!Number.isFinite(targetReplicas) || targetReplicas <= 0) return "";
    const current = Number(currentReplicas(item?.spec || {}));
    if (!Number.isFinite(current) || current <= 0 || current === targetReplicas) {
      return `目标副本 ${formatNumber(targetReplicas, 0)}`;
    }
    const label = targetReplicas > current ? "扩副本" : "缩副本";
    return `${label} ${formatNumber(current, 0)} → ${formatNumber(targetReplicas, 0)}`;
  }

  function representativeK8sMetricStats(item, baseKey, action) {
    const direct = metricStatsFor(item, baseKey);
    if (direct.p95 !== undefined) return direct;
    const preferredKey = action === "scale_out_candidate"
      ? `${baseKey}_limit`
      : action === "scale_in_candidate"
        ? `${baseKey}_request`
        : `${baseKey}_limit`;
    const preferred = metricStatsFor(item, preferredKey);
    if (preferred.p95 !== undefined) return preferred;
    return metricKeysFor(item)
      .filter((key) => baseMetricKey(key) === baseKey)
      .map((key) => metricStatsFor(item, key))
      .find((stat) => stat.p95 !== undefined) || {};
  }

  function k8sMetricSummary(item) {
    const chips = [];
    const overallAction = actionOf(item);
    const replicaText = replicaTargetSummary(item);
    if (replicaText) {
      chips.push(`<span class="metric-pill is-${escapeHtml(overallAction)}">${escapeHtml(replicaText)}</span>`);
    }
    ["cpu", "memory"].forEach((baseKey) => {
      const action = metricActionFor(item, baseKey);
      const stat = representativeK8sMetricStats(item, baseKey, action);
      const unit = resolveDisplayUnit(item, baseKey);
      const p95 = stat.p95 !== undefined ? `P95 ${formatStatValue(stat.p95, unit)}` : actionLabel(action);
      const label = baseKey === "cpu" ? "CPU" : "内存";
      chips.push(`<span class="metric-pill is-${escapeHtml(action)}">${escapeHtml(label)} ${escapeHtml(p95)}</span>`);
    });
    return chips.join("");
  }

  function triggerMetric(item) {
    return metricKeysFor(item).find((key) => metricActionFor(item, key) !== "hold") || metricKeysFor(item)[0];
  }

  function metricSummary(item) {
    if (isK8s(item)) return k8sMetricSummary(item);
    return metricKeysFor(item).map((key) => {
      const stat = metricStatsFor(item, key);
      const action = metricActionFor(item, key);
      const label = metricActionLabel(item, key, action);
      const unit = resolveDisplayUnit(item, key);
      const p95 = stat.p95 !== undefined ? `P95 ${formatStatValue(stat.p95, unit)}` : "";
      return `<span class="metric-pill is-${escapeHtml(action)}">${escapeHtml(app.metricTitleMap[key])} ${escapeHtml(label)}${p95 ? ` · ${escapeHtml(p95)}` : ""}</span>`;
    }).join("");
  }

  function targetSpecText(item) {
    const advice = item?.scaling_advice || {};
    if (isK8s(item)) {
      const target = advice.target_spec || {};
      // 只在值真正存在时才显示；null/undefined 时该字段留空，不显示占位符
      const cpuReq = target.cpu_request_cores != null ? formatNumber(target.cpu_request_cores, 2) : null;
      const cpuLimit = target.cpu_limit_cores != null ? formatNumber(target.cpu_limit_cores, 2) : null;
      const memReq = target.memory_request_gb != null ? formatMemoryGiB(target.memory_request_gb, 2) : null;
      const memLimit = target.memory_limit_gb != null ? formatMemoryGiB(target.memory_limit_gb, 2) : null;
      const replicas = target.replicas != null ? formatNumber(target.replicas, 0) : null;
      const parts = [];
      // CPU 行：有 request 或 limit 才显示，缺失的一方留空
      if (cpuReq || cpuLimit) {
        let cpuText = "";
        if (cpuReq) cpuText += `CPU request ${cpuReq}C`;
        if (cpuLimit) cpuText += `${cpuText ? " / " : "CPU "}limit ${cpuLimit}C`;
        parts.push(cpuText);
      }
      // 内存行：同理
      if (memReq || memLimit) {
        let memText = "";
        if (memReq) memText += `内存 request ${memReq}`;
        if (memLimit) memText += `${memText ? " / " : "内存 "}limit ${memLimit}`;
        parts.push(memText);
      }
      if (replicas) parts.push(`副本 ${replicas}`);
      if (parts.length) return `目标 ${parts.join(" · ")}`;
      return advice.analysis_only ? "仅分析，缺少可执行目标" : "K8S 目标待确认";
    }
    const target = advice.target_spec || {};
    const hasCpu = target.cpu_cores != null && Number.isFinite(Number(target.cpu_cores));
    const hasMem = target.memory_gb != null && Number.isFinite(Number(target.memory_gb));
    const hasDisk = target.disk_gb != null && Number.isFinite(Number(target.disk_gb));
    if (!hasCpu && !hasMem && !hasDisk) return "目标规格待确认";
    const parts = [];
    if (hasCpu) parts.push(`${formatNumber(target.cpu_cores, 0)}C`);
    if (hasMem) parts.push(`${formatNumber(target.memory_gb, 0)}GB`);
    if (hasDisk) parts.push(`${formatNumber(target.disk_gb, 0)}GB`);
    return `目标规格 ${parts.join(" / ")}`;
  }

  function positiveNumber(value) {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  function sumContainerSpec(spec, field) {
    const containers = spec?.containers || {};
    if (!containers || typeof containers !== "object") return null;
    let total = 0;
    let found = false;
    Object.values(containers).forEach((values) => {
      if (!values || typeof values !== "object") return;
      const n = positiveNumber(values[field]);
      if (n === null) return;
      total += n;
      found = true;
    });
    return found ? total : null;
  }

  function supportsReplicaScaling(spec) {
    const kind = String(spec?.workload_kind || spec?.owner_kind || "").trim().toLowerCase().replaceAll("-", "");
    return ["deployment", "statefulset", "replicaset"].includes(kind);
  }

  function translatedPolicyNote(note) {
    const raw = String(note || "").trim();
    if (!raw) return "";
    if (raw.includes("lacks request/limit baseline")) return raw.replace(
      /^(.+) lacks request\/limit baseline; recommendation is trend-only$/,
      "$1 缺少 request/limit 基线，当前建议仅作为趋势分析"
    ).replace("CPU", "CPU").replace("memory", "内存");
    if (raw.includes("DaemonSet replicas follow node scheduling")) {
      return "DaemonSet 副本跟随节点调度，不能直接生成副本缩放目标。";
    }
    if (raw.includes("consider HPA")) return "CPU 触发扩容时更适合结合 HPA 策略评估。";
    if (raw.includes("Total")) return raw;
    return "";
  }

  function analysisOnlyReasons(item) {
    const advice = item?.scaling_advice || {};
    if (!isK8s(item) || !advice.analysis_only) return [];
    const spec = item?.spec || {};
    const action = actionOf(item);
    const metricActions = advice.metric_actions || {};
    const reasons = [];
    const notes = advice.target_k8s_policy?.notes;
    if (Array.isArray(notes)) {
      notes.map(translatedPolicyNote).filter(Boolean).forEach((note) => reasons.push(note));
    }
    if (action === "scale_in_candidate") {
      const replicas = currentReplicas(spec);
      if (supportsReplicaScaling(spec) && replicas !== null && replicas <= 1) {
        reasons.push(`当前副本数为 ${formatNumber(replicas, 0)}，副本缩容不能低于 1。`);
      }
      if (metricActions.cpu === "scale_in_candidate") {
        const cpuReq = sumContainerSpec(spec, "cpu_request_cores");
        if (cpuReq === null) {
          reasons.push("CPU 缺少 request 基线，无法计算 CPU 缩容目标。");
        } else if (cpuReq < 2) {
          reasons.push(`CPU request 基线为 ${formatNumber(cpuReq, 2)}C，低于 2C；小规格 Workload 不继续下调 request。`);
        }
      }
      if (metricActions.memory === "scale_in_candidate") {
        const memReq = sumContainerSpec(spec, "memory_request_gb");
        if (memReq === null) {
          reasons.push("内存缺少 request 基线，无法计算内存缩容目标。");
        } else if (memReq < 2) {
          reasons.push(`内存 request 基线为 ${formatMemoryGiB(memReq, 2)}，低于 2Gi；小规格 Workload 不继续下调 request。`);
        }
      }
    } else if (action === "scale_out_candidate") {
      if (metricActions.cpu === "scale_out_candidate" && sumContainerSpec(spec, "cpu_limit_cores") === null) {
        reasons.push("CPU 缺少 limit 基线，无法计算 CPU 扩容目标。");
      }
      if (metricActions.memory === "scale_out_candidate" && sumContainerSpec(spec, "memory_limit_gb") === null) {
        reasons.push("内存缺少 limit 基线，无法计算内存扩容目标。");
      }
    }
    if (!reasons.length && advice.target_k8s_policy?.ready_for_execution === false) {
      reasons.push("后端未生成可执行 target_spec，当前建议仅作为风险分析参考。");
    }
    return Array.from(new Set(reasons));
  }

  function currentReplicas(spec) {
    // 调配成功后 snapshot 会把目标副本数写入 spec.replicas，
    // 但 spec.replicas_observed 需要等到下次数据拉取才会同步。
    // 优先显示 spec.replicas，保证调配后前端立即反映真实副本数。
    const fromReplicas = spec.replicas !== undefined && spec.replicas !== null && spec.replicas !== ""
      ? Number(spec.replicas) : null;
    const fromObserved = spec.replicas_observed !== undefined && spec.replicas_observed !== null && spec.replicas_observed !== ""
      ? Number(spec.replicas_observed) : null;
    // 若两者均存在且不一致，取较大值（调配刚完成时 replicas > replicas_observed）
    if (fromReplicas !== null && fromObserved !== null) return Math.max(fromReplicas, fromObserved);
    return fromReplicas ?? fromObserved;
  }

  function subtitleFor(item) {
    const spec = item?.spec || {};
    if (isK8s(item)) {
      const replicas = currentReplicas(spec);
      return [
        spec.cluster,
        spec.namespace,
        [spec.workload_kind || spec.owner_kind, spec.workload_name || spec.owner_name].filter(Boolean).join("/"),
        replicas !== null && replicas !== undefined ? `${formatNumber(replicas, 0)} 副本` : "",
      ].filter(Boolean).join(" / ") || "-";
    }
    const parts = [
      spec.cluster,
      spec.ip,
    ].filter(Boolean);
    if (spec.cpu_cores != null && Number.isFinite(Number(spec.cpu_cores))) parts.push(`${formatNumber(spec.cpu_cores, 0)}C`);
    if (spec.memory_gb != null && Number.isFinite(Number(spec.memory_gb))) parts.push(`${formatNumber(spec.memory_gb, 0)}GB`);
    if (spec.disk_gb != null && Number.isFinite(Number(spec.disk_gb))) parts.push(`${formatNumber(spec.disk_gb, 0)}GB`);
    return parts.join(" / ") || "-";
  }

  function titleFor(item) {
    const spec = item?.spec || {};
    if (isK8s(item)) {
      return spec.workload_name || spec.owner_name || spec.pod || item.resource_id || "-";
    }
    return item.resource_id || "-";
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
    return app.state.visibleItems;
  }

  function updatePager() {
    const totalPages = Math.max(1, Math.ceil(app.state.total / app.state.pageSize));
    app.state.page = Math.min(app.state.page, totalPages);
    app.els.prevPageBtn.disabled = app.state.page <= 1;
    app.els.nextPageBtn.disabled = app.state.page >= totalPages;
    app.els.pagerText.textContent = `第 ${app.state.page} / ${totalPages} 页`;
  }

  function updateSummary() {
    const summaryItems = applyClientFilters(app.state.loadedItems, { ignoreAction: true });
    const counts = { scale_out: 0, scale_in: 0, mixed: 0, hold: 0 };
    const summaryCounts = app.state.adviceSummary?.action_counts;
    const summaryTotal = Number(app.state.adviceSummary?.total ?? 0) || 0;
    if (summaryCounts) {
      counts.scale_out = Number(summaryCounts.scale_out || 0) + Number(summaryCounts.scale_out_candidate || 0);
      counts.scale_in = Number(summaryCounts.scale_in || 0) + Number(summaryCounts.scale_in_candidate || 0);
      counts.mixed = Number(summaryCounts.mixed || 0);
      counts.hold = Number(summaryCounts.hold || 0);
    } else {
      for (const item of summaryItems) {
        const action = actionOf(item);
        if (action === "scale_out" || action === "scale_out_candidate") counts.scale_out += 1;
        else if (action === "scale_in" || action === "scale_in_candidate") counts.scale_in += 1;
        else if (action === "mixed") counts.mixed += 1;
        else counts.hold += 1;
      }
    }
    app.els.sumOut.textContent = String(counts.scale_out);
    app.els.sumIn.textContent = String(counts.scale_in);
    app.els.sumMixed.textContent = String(counts.mixed);
    app.els.sumHold.textContent = String(counts.hold);
    app.els.sumTotal.textContent = String(summaryTotal || app.state.total || summaryItems.length);
    app.els.summaryText.textContent = `匹配 ${app.state.total || 0} 个资源，当前页显示 ${currentPageItems().length} 个`;
    app.els.summaryItems.forEach((el) => {
      el.classList.toggle("active", (el.dataset.summaryAction || "") === app.state.actionFilter);
    });
    renderOverview(counts, summaryItems);
  }

  function activeForecastMethods() {
    const forecastConfig = app.state.forecastConfigPayload || {};
    const methods = Array.isArray(forecastConfig.enabled_methods)
      ? forecastConfig.enabled_methods.filter((method) => typeof method === "string" && method)
      : [];
    if (forecastConfig.enable_ensemble && !methods.includes("ensemble")) {
      methods.push("ensemble");
    }
    return methods;
  }

  function bestMethodCounts(summaryItems) {
    const methods = activeForecastMethods();
    const enabled = new Set(methods);
    const counts = Object.fromEntries(methods.map((method) => [method, 0]));
    for (const item of summaryItems) {
      const bestMethods = item?.best_methods || {};
      if (!bestMethods || typeof bestMethods !== "object") continue;
      Object.values(bestMethods).forEach((method) => {
        if (enabled.has(method)) counts[method] += 1;
      });
    }
    return methods.map((method) => [
      `${app.labelMap[method] || method} 最优`,
      counts[method] || 0,
    ]);
  }

  function renderOverview(counts, summaryItems) {
    if (!app.els.overviewGrid) return;
    const byType = summaryItems.reduce((acc, item) => {
      acc[typeLabel(item)] = (acc[typeLabel(item)] || 0) + 1;
      return acc;
    }, {});
    const overviewCards = [
      ["匹配总数", app.state.total || summaryItems.length],
      ["当前页", summaryItems.length],
      ["VM", byType.VM || 0],
      ["Workload", byType.Workload || 0],
      ["需扩容", counts.scale_out],
      ["需缩容", counts.scale_in],
      ["混合信号", counts.mixed],
      ...bestMethodCounts(summaryItems),
    ];
    app.els.overviewGrid.innerHTML = overviewCards
      .map(([k, v]) => `<div class="overview-card"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`)
      .join("");
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
            <strong title="${escapeHtml(item.resource_id)}">${escapeHtml(titleFor(item))}</strong>
            <span class="resource-type-badge">${typeLabel(item)}</span>
            <span class="action-chip is-${escapeHtml(action)}">${escapeHtml(actionLabel(action))}</span>
          </span>
          <span class="row-subtitle">${escapeHtml(subtitleFor(item))}</span>
          <span class="row-metrics">${metricSummary(item)}</span>
        </span>
        <span class="row-side">
          <span class="score-block">
            <span class="score-label">紧急度 ${infoTooltip(URGENCY_HELP, "紧急度计算说明")}</span>
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

  function setItems(items, meta = {}) {
    app.state.loadedItems = items || [];
    app.state.total = Number(meta.total ?? app.state.loadedItems.length) || 0;
    app.state.page = Number(meta.page ?? app.state.page) || 1;
    app.state.pageSize = Number(meta.page_size ?? app.state.pageSize) || app.state.pageSize;
    app.state.visibleItems = applyClientFilters(app.state.loadedItems);
    if (app.state.page < 1) app.state.page = 1;
    renderRows();
    if (!app.state.selectedResourceId && app.state.visibleItems.length) {
      hideDetail();
    } else if (app.state.selectedResourceId && !app.state.visibleItems.some((x) => x.resource_id === app.state.selectedResourceId)) {
      app.state.selectedResourceId = "";
      hideDetail();
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
    CONFIDENCE_LABELS,
    CONFIDENCE_HELP,
    actionLabel,
    actionOf,
    applyClientFilters,
    confidenceOf,
    escapeHtml,
    formatNumber,
    formatStatValue,
    formatMemoryGiB,
    infoTooltip,
    isK8s,
    analysisOnlyReasons,
    metricActionFor,
    metricKeysFor,
    metricStatsFor,
    renderRows,
    resolveDisplayUnit,
    resourceTypeOf,
    selectResource,
    setItems,
    subtitleFor,
    syncFilterButtons,
    targetSpecText,
    titleFor,
    triggerMetric,
    typeLabel,
  };
})();
