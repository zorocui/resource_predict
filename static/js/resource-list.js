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
    const keys = app.viewMetricMap[type] || app.viewMetricMap.openstack_vm;
    if (type !== "k8s_workload") return keys;
    const seenRawBases = new Set();
    return keys.filter((key) => {
      const unit = resolveDisplayUnit(item, key);
      if (unit === "percent") return true;
      const base = baseMetricKey(key);
      if (seenRawBases.has(base)) return false;
      seenRawBases.add(base);
      return true;
    });
  }

  function metricTitleFor(item, metricKey) {
    if (isK8s(item) && resolveDisplayUnit(item, metricKey) !== "percent") {
      if (String(metricKey).startsWith("cpu_")) return "CPU 使用量";
      if (String(metricKey).startsWith("memory_")) return "内存使用量";
    }
    return app.metricTitleMap[metricKey] || metricKey;
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
    return String(Number(n.toFixed(digits)));
  }

  function formatCpuCores(value, digits = 2) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    if (Math.abs(n) > 0 && Math.abs(n) < 1) return `${formatNumber(n * 1000, 0)}m`;
    return `${formatNumber(n, digits)}C`;
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

  function metricObservedStatsFor(item, metricKey) {
    const containerStats = containerMetricObservedStatsFor(item, metricKey);
    if (containerStats) return containerStats;
    const observedStats = item?.observed_stats || {};
    const observed = observedStats[metricKey];
    if (observed && typeof observed === "object" && observed.p95 !== undefined) return observed;
    const chart = item?.charts?.[metricKey] || {};
    return chartObservedStats(chart);
  }

  function containerMetricObservedStatsFor(item, metricKey, containerName = "") {
    if (!isK8s(item)) return null;
    const name = containerName || representativeContainerName(item, metricKey);
    if (!name) return null;
    const chart = item?.container_charts?.[name]?.[metricKey];
    return chartObservedStats(chart);
  }

  function representativeContainerName(item, metricKey) {
    const targetContainers = item?.scaling_advice?.target_spec?.containers;
    if (targetContainers && typeof targetContainers === "object" && !Array.isArray(targetContainers)) {
      const targetNames = Object.keys(targetContainers).filter((name) => {
        const values = targetContainers[name];
        return values && typeof values === "object" && Object.keys(values).length;
      });
      if (targetNames.length === 1) return targetNames[0];
      if (targetNames.length > 1) {
        const withMetric = targetNames.find((name) => item?.container_charts?.[name]?.[metricKey]);
        if (withMetric) return withMetric;
        return targetNames[0];
      }
    }
    const charts = item?.container_charts;
    if (charts && typeof charts === "object" && !Array.isArray(charts)) {
      return Object.keys(charts).find((name) => charts[name]?.[metricKey]) || "";
    }
    return "";
  }

  function chartObservedStats(chart) {
    if (!chart || typeof chart !== "object") return null;
    const values = []
      .concat(Array.isArray(chart.y_train) ? chart.y_train : [])
      .concat(Array.isArray(chart.y_test) ? chart.y_test : [])
      .map((value) => Number(value))
      .filter((value) => Number.isFinite(value));
    if (!values.length) return null;
    values.sort((a, b) => a - b);
    const sum = values.reduce((acc, value) => acc + value, 0);
    return {
      avg: sum / values.length,
      p95: percentile(values, 95),
      peak: values[values.length - 1],
    };
  }

  function percentile(sortedValues, pct) {
    if (!sortedValues.length) return 0;
    if (sortedValues.length === 1) return sortedValues[0];
    const rank = (pct / 100) * (sortedValues.length - 1);
    const low = Math.floor(rank);
    const high = Math.ceil(rank);
    if (low === high) return sortedValues[low];
    const weight = rank - low;
    return sortedValues[low] * (1 - weight) + sortedValues[high] * weight;
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
    const preferredKey = representativeK8sMetricKey(item, baseKey, action);
    const preferredObserved = metricObservedStatsFor(item, preferredKey);
    if (preferredObserved?.p95 !== undefined) return { key: preferredKey, stats: preferredObserved };
    const preferred = metricStatsFor(item, preferredKey);
    if (preferred.p95 !== undefined) return { key: preferredKey, stats: preferred };
    const fallbackKey = metricKeysFor(item)
      .filter((key) => baseMetricKey(key) === baseKey)
      .find((key) => metricObservedStatsFor(item, key)?.p95 !== undefined || metricStatsFor(item, key).p95 !== undefined);
    if (fallbackKey) return { key: fallbackKey, stats: metricObservedStatsFor(item, fallbackKey) || metricStatsFor(item, fallbackKey) };
    const direct = metricStatsFor(item, baseKey);
    return { key: baseKey, stats: direct.p95 !== undefined ? direct : {} };
  }

  function representativeK8sMetricKey(item, baseKey, action) {
    if (action === "scale_out_candidate") return `${baseKey}_limit`;
    if (action === "scale_in_candidate") return `${baseKey}_request`;
    const limitKey = `${baseKey}_limit`;
    const requestKey = `${baseKey}_request`;
    const limitUnit = resolveDisplayUnit(item, limitKey);
    const requestUnit = resolveDisplayUnit(item, requestKey);
    if (limitUnit !== "percent") return limitKey;
    if (requestUnit !== "percent") return requestKey;
    return limitKey;
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
      const representative = representativeK8sMetricStats(item, baseKey, action);
      const stat = representative.stats;
      const unit = resolveDisplayUnit(item, representative.key);
      const p95 = stat.p95 !== undefined ? `P95 ${formatStatValue(stat.p95, unit)}` : actionLabel(action);
      const label = metricTitleFor(item, representative.key);
      chips.push(`<span class="metric-pill is-${escapeHtml(action)}">${escapeHtml(label)} ${escapeHtml(p95)}</span>`);
    });
    return chips.join("");
  }

  function triggerMetric(item) {
    if (isK8s(item)) {
      const keys = metricKeysFor(item);
      for (const baseKey of ["cpu", "memory"]) {
        const action = metricActionFor(item, baseKey);
        if (action === "hold") continue;
        const preferredKey = representativeK8sMetricKey(item, baseKey, action);
        if (keys.includes(preferredKey)) return preferredKey;
      }
    }
    return metricKeysFor(item).find((key) => metricActionFor(item, key) !== "hold") || metricKeysFor(item)[0];
  }

  function metricSummary(item) {
    if (isK8s(item)) return k8sMetricSummary(item);
    return metricKeysFor(item).map((key) => {
      const stat = metricObservedStatsFor(item, key) || metricStatsFor(item, key);
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
      const containerLines = formatTargetContainers(target.containers);
      if (containerLines.length) {
        const replicas = target.replicas != null ? formatNumber(target.replicas, 0) : null;
        return [
          "K8S 目标",
          ...containerLines,
          ...(replicas ? [`副本 ${replicas}`] : []),
        ].join("\n");
      }
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

  function targetSpecRows(item) {
    const advice = item?.scaling_advice || {};
    const target = advice.target_spec || {};
    if (isK8s(item)) {
      const containerLines = formatTargetContainers(target.containers);
      if (containerLines.length) {
        const rows = containerLines.map((line) => {
          const [name, ...rest] = String(line).split(": ");
          return { label: name || "容器", value: rest.join(": ") || line };
        });
        if (target.replicas != null) rows.push({ label: "副本", value: formatNumber(target.replicas, 0) });
        return rows;
      }
      const rows = [];
      const cpuFields = [];
      const memFields = [];
      const cpuReq = target.cpu_request_cores != null ? formatNumber(target.cpu_request_cores, 2) : null;
      const cpuLimit = target.cpu_limit_cores != null ? formatNumber(target.cpu_limit_cores, 2) : null;
      const memReq = target.memory_request_gb != null ? formatMemoryGiB(target.memory_request_gb, 2) : null;
      const memLimit = target.memory_limit_gb != null ? formatMemoryGiB(target.memory_limit_gb, 2) : null;
      if (cpuReq) cpuFields.push(`Request ${cpuReq}C`);
      if (cpuLimit) cpuFields.push(`Limit ${cpuLimit}C`);
      if (memReq) memFields.push(`Request ${memReq}`);
      if (memLimit) memFields.push(`Limit ${memLimit}`);
      if (cpuFields.length) rows.push({ label: "CPU", value: cpuFields.join(" / ") });
      if (memFields.length) rows.push({ label: "内存", value: memFields.join(" / ") });
      if (target.replicas != null) rows.push({ label: "副本", value: formatNumber(target.replicas, 0) });
      return rows;
    }
    const rows = [];
    if (target.cpu_cores != null && Number.isFinite(Number(target.cpu_cores))) rows.push({ label: "CPU", value: `${formatNumber(target.cpu_cores, 0)}C` });
    if (target.memory_gb != null && Number.isFinite(Number(target.memory_gb))) rows.push({ label: "内存", value: `${formatNumber(target.memory_gb, 0)}GB` });
    if (target.disk_gb != null && Number.isFinite(Number(target.disk_gb))) rows.push({ label: "磁盘", value: `${formatNumber(target.disk_gb, 0)}GB` });
    return rows;
  }

  function targetSpecDetailMarkup(item) {
    const advice = item?.scaling_advice || {};
    const target = advice.target_spec || {};
    if (isK8s(item) && target.containers && typeof target.containers === "object" && !Array.isArray(target.containers)) {
      const groups = targetContainerGroups(target.containers);
      if (groups.length) {
        const replica = target.replicas != null ? `
          <span class="target-result-item target-result-replica">
            <b>副本</b>
            <em>${escapeHtml(formatNumber(target.replicas, 0))}</em>
          </span>` : "";
        return `
          <div class="target-container-grid">
            ${groups.map((group) => `
              <section class="target-container-card">
                <header title="${escapeHtml(group.name)}">${escapeHtml(group.name)}</header>
                <div class="target-container-matrix">
                  ${targetContainerResourceRow("CPU", group.cpu)}
                  ${targetContainerResourceRow("内存", group.memory)}
                </div>
              </section>
            `).join("")}
            ${replica}
          </div>`;
      }
    }
    const rows = targetSpecRows(item);
    if (!rows.length) return `<span class="target-result-empty">${escapeHtml(targetSpecText(item))}</span>`;
    return rows.map((row) => `
      <span class="target-result-item">
        <b>${escapeHtml(row.label)}</b>
        <em>${escapeHtml(row.value)}</em>
      </span>`).join("");
  }

  function formatTargetContainers(containers) {
    if (!containers || typeof containers !== "object" || Array.isArray(containers)) return [];
    return Object.entries(containers)
      .filter(([, values]) => values && typeof values === "object")
      .map(([name, values]) => {
        const fields = [];
        const cpuReq = values.cpu_request_cores != null ? formatCpuCores(values.cpu_request_cores, 2) : null;
        const cpuLimit = values.cpu_limit_cores != null ? formatCpuCores(values.cpu_limit_cores, 2) : null;
        const memReq = values.memory_request_gb != null ? formatMemoryGiB(values.memory_request_gb, 2) : null;
        const memLimit = values.memory_limit_gb != null ? formatMemoryGiB(values.memory_limit_gb, 2) : null;
        if (cpuReq) fields.push(`CPU req ${cpuReq}`);
        if (cpuLimit) fields.push(`CPU limit ${cpuLimit}`);
        if (memReq) fields.push(`内存 req ${memReq}`);
        if (memLimit) fields.push(`内存 limit ${memLimit}`);
        return fields.length ? `${name}: ${fields.join(" / ")}` : "";
      })
      .filter(Boolean);
  }

  function targetContainerGroups(containers) {
    if (!containers || typeof containers !== "object" || Array.isArray(containers)) return [];
    return Object.entries(containers)
      .filter(([, values]) => values && typeof values === "object")
      .map(([name, values]) => {
        const cpu = [];
        const memory = [];
        if (values.cpu_request_cores != null) cpu.push({ label: "request", value: formatCpuCores(values.cpu_request_cores, 2) });
        if (values.cpu_limit_cores != null) cpu.push({ label: "limit", value: formatCpuCores(values.cpu_limit_cores, 2) });
        if (values.memory_request_gb != null) memory.push({ label: "request", value: formatMemoryGiB(values.memory_request_gb, 2) });
        if (values.memory_limit_gb != null) memory.push({ label: "limit", value: formatMemoryGiB(values.memory_limit_gb, 2) });
        return { name, cpu, memory };
      })
      .filter((group) => group.cpu.length || group.memory.length);
  }

  function targetContainerResourceRow(label, fields) {
    if (!Array.isArray(fields) || !fields.length) return "";
    const byLabel = new Map(fields.map((field) => [field.label, field.value]));
    const cell = (fieldLabel) => {
      const value = byLabel.get(fieldLabel);
      const emptyClass = value == null ? " is-empty" : "";
      return `<span class="target-resource-value${emptyClass}"><b>${escapeHtml(fieldLabel)}</b><em>${escapeHtml(value || "-")}</em></span>`;
    };
    return `
      <span class="target-resource-label">${escapeHtml(label)}</span>
      ${cell("request")}
      ${cell("limit")}
    `;
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
      return spec.workload_name || spec.owner_name || item.resource_id || "-";
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

  function bestMethodCounts(summaryItems, summaryPayload = null) {
    const methods = activeForecastMethods();
    const backendCounts = summaryPayload?.best_method_counts;
    if (backendCounts && typeof backendCounts === "object") {
      return methods.map((method) => [
        `${app.labelMap[method] || method} 最优`,
        Number(backendCounts[method] || 0),
      ]);
    }
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
    const overview = app.state.overviewSummary || {};
    const overviewCounts = overview.action_counts || {};
    const overviewTypeCounts = overview.resource_type_counts || {};
    const hasOverview = Number.isFinite(Number(overview.total));
    const byType = hasOverview
      ? {
        VM: Number(overviewTypeCounts.openstack_vm || 0),
        Workload: Number(overviewTypeCounts.k8s_workload || 0),
      }
      : summaryItems.reduce((acc, item) => {
        acc[typeLabel(item)] = (acc[typeLabel(item)] || 0) + 1;
        return acc;
      }, {});
    const actionCounts = hasOverview
      ? {
        scale_out: Number(overviewCounts.scale_out || 0) + Number(overviewCounts.scale_out_candidate || 0),
        scale_in: Number(overviewCounts.scale_in || 0) + Number(overviewCounts.scale_in_candidate || 0),
        mixed: Number(overviewCounts.mixed || 0),
      }
      : counts;
    const overviewCards = [
      ["匹配总数", hasOverview ? Number(overview.total || 0) : (app.state.total || summaryItems.length)],
      ["当前页", currentPageItems().length],
      ["VM", byType.VM || 0],
      ["Workload", byType.Workload || 0],
      ["需扩容", actionCounts.scale_out || 0],
      ["需缩容", actionCounts.scale_in || 0],
      ["混合信号", actionCounts.mixed || 0],
      ...bestMethodCounts(summaryItems, overview),
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
    metricTitleFor,
    metricObservedStatsFor,
    containerMetricObservedStatsFor,
    representativeContainerName,
    metricStatsFor,
    renderRows,
    resolveDisplayUnit,
    resourceTypeOf,
    selectResource,
    setItems,
    subtitleFor,
    syncFilterButtons,
    targetSpecDetailMarkup,
    targetSpecText,
    titleFor,
    triggerMetric,
    typeLabel,
  };
})();
