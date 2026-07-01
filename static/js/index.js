(function () {
  const app = window.ResourcePredictApp;
  const api = window.ResourceApi;
  const charts = window.ResourceCharts;
  const list = window.ResourceList;
  let updatePollTimer = null;
  let updatePollWasRunning = false;

  function setView(view) {
    app.state.activeView = view || "risk";
    app.els.navTabs.forEach((tab) => {
      const active = tab.dataset.view === app.state.activeView;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-pressed", active ? "true" : "false");
    });
    app.els.viewPanels.forEach((panel) => {
      panel.classList.toggle("active", panel.id === `${app.state.activeView}-view`);
    });
    if (app.state.activeView === "tasks") renderTaskPanel();
    if (app.state.activeView === "updates") {
      refreshUpdateStatus();
      refreshUpdateHistory();
    }
    if (app.state.activeView === "configs") refreshClusterConfigs();
  }

  function setDetailTab(tab) {
    const nextTab = tab || "summary";
    app.els.detailTabs.forEach((btn) => {
      const active = btn.dataset.detailTab === nextTab;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    app.els.detailTabPanels.forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.detailPanel === nextTab);
    });
    if (nextTab === "metrics") {
      requestAnimationFrame(() => app.detailChartInstance?.resize());
    }
  }

  async function loadQueue({ keepSelection = false } = {}) {
    app.els.summaryText.textContent = "正在加载资源...";
    app.resourcePayloadCache.clear();
    app.chartDataByKey.clear();
    app.loadedChartKeys.clear();
    const [payload, summaryPayload, overviewPayload, forecastPayload] = await Promise.all([
      api.requestJson(api.buildQuery("/api/resources", {
        page: app.state.page,
        page_size: app.state.pageSize,
        sort_by: "urgency_score",
        resource_type: app.state.resourceTypeFilter,
        action: app.state.actionFilter,
        confidence: app.state.confidenceFilter,
        q: app.state.query,
      })),
      api.requestJson(api.buildQuery("/api/resources/advice-summary", {
        resource_type: app.state.resourceTypeFilter,
        confidence: app.state.confidenceFilter,
        q: app.state.query,
      })),
      api.requestJson(api.buildQuery("/api/resources/advice-summary", {
        resource_type: app.state.resourceTypeFilter,
        action: app.state.actionFilter,
        confidence: app.state.confidenceFilter,
        q: app.state.query,
      })),
      api.requestJson("/api/forecast-config", 1),
    ]);
    app.state.adviceSummary = summaryPayload;
    app.state.overviewSummary = overviewPayload;
    app.state.forecastConfigPayload = forecastPayload;
    const items = payload.items || [];
    if (!keepSelection) app.state.selectedResourceId = "";
    list.setItems(items, payload);
    list.syncFilterButtons();
  }

  function applyActionFilter(value, options = {}) {
    const nextValue = value || "";
    app.state.actionFilter = options.toggle && app.state.actionFilter === nextValue ? "" : nextValue;
    app.state.page = 1;
    loadQueue();
  }

  function applyConfidenceFilter(value) {
    app.state.confidenceFilter = value || "";
    app.state.page = 1;
    loadQueue();
  }

  function resetFilters() {
    app.state.resourceTypeFilter = "";
    app.state.actionFilter = "";
    app.state.confidenceFilter = "";
    app.state.query = "";
    app.state.page = 1;
    app.els.searchInput.value = "";
    list.syncFilterButtons();
    loadQueue();
  }

  function selectedResource() {
    return app.state.loadedItems.find((item) => String(item.resource_id || "") === app.state.selectedResourceId) || null;
  }

  function formatDateTime(value) {
    if (!value) return "-";
    let normalized = value;
    if (typeof normalized === "number" && normalized > 0 && normalized < 1e12) {
      normalized *= 1000;
    }
    const d = new Date(normalized);
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(d);
  }

  const TASK_STATUS_LABELS = {
    queued: "排队中",
    running: "执行中",
    waiting_confirm: "等待确认",
    confirming: "确认中",
    success: "成功",
    failed: "失败",
  };

  const TASK_MODE_LABELS = {
    dry_run: "预检",
    execute: "调配",
  };

  function renderTaskHistory(tasks) {
    if (!app.els.taskHistory) return;
    if (!tasks.length) {
      app.els.taskHistory.innerHTML = `<div class="empty-list is-compact">当前资源还没有调配记录。</div>`;
      return;
    }
    app.els.taskHistory.innerHTML = tasks.map((task) => {
      const status = String(task.status || "");
      const statusLabel = TASK_STATUS_LABELS[status] || status || "-";
      const modeLabel = TASK_MODE_LABELS[String(task.mode || "")] || task.mode || "调配任务";
      const plan = task.plan || {};
      const actionLabel = plan.action ? `（${list.escapeHtml(plan.action)}）` : "";
      const createdAt = task.created_at_ms || task.updated_at_ms || "";
      return `
      <div class="task-item">
        <div>
          <strong>${list.escapeHtml(modeLabel)}${actionLabel}</strong>
          <span>${list.escapeHtml(task.task_id || "")}</span>
        </div>
        <div>
          <span class="task-status is-${list.escapeHtml(status)}">${list.escapeHtml(statusLabel)}</span>
          <small>${list.escapeHtml(formatDateTime(createdAt))}</small>
        </div>
      </div>
    `;
    }).join("");
  }

  async function renderTaskPanel() {
    if (!app.els.taskResource || !app.els.taskCapability || !app.els.taskHistory) return;
    const resource = selectedResource();
    if (!resource) {
      app.els.taskResource.textContent = "未选择";
      app.els.taskCapability.textContent = "-";
      app.els.taskHistory.innerHTML = `<div class="empty-list is-compact">先在风险队列中选择一个资源。</div>`;
      return;
    }
    app.els.taskResource.textContent = resource.resource_id || "-";
    app.els.taskCapability.textContent = list.isK8s(resource) ? "K8S 可预检 / 可调配" : "可预检 / 可调配";
    app.els.taskHistory.innerHTML = `<div class="empty-list is-compact">正在读取调配记录...</div>`;
    try {
      const payload = await api.requestJson(`/api/resources/${encodeURIComponent(resource.resource_id)}/scaling-history?limit=8`, 1);
      renderTaskHistory(payload.tasks || []);
    } catch (e) {
      app.els.taskHistory.innerHTML = `<div class="empty-list is-compact">调配记录读取失败：${list.escapeHtml(e.message || e)}</div>`;
    }
  }

  function updateStatusText(status) {
    if (!app.els.updateRunning) return;
    const running = Boolean(status?.running);
    app.els.updateRunning.textContent = running ? "运行中" : "空闲";
    app.els.updatePhase.textContent = status?.phase || status?.step || "-";
    if (app.els.updateSource) app.els.updateSource.textContent = status?.task_source || "-";
    if (app.els.updateWindow) app.els.updateWindow.textContent = status?.fetch_window_label || "-";
    app.els.updateStarted.textContent = formatDateTime(status?.started_at || status?.start_time || status?.last_started_at);
    app.els.updateFinished.textContent = formatDateTime(status?.finished_at || status?.end_time || status?.last_success_at || status?.last_finished_at);
    const message = status?.last_error || status?.error || status?.message || "";
    app.els.updateMessage.textContent = message ? String(message) : "暂无更新消息。";
  }

  async function refreshUpdateStatus() {
    if (!app.els.updateRunning) return;
    app.els.updateRunning.textContent = "读取中";
    try {
      const status = await api.requestJson("/api/update-status", 1);
      updateStatusText(status);
      return status;
    } catch (e) {
      app.els.updateRunning.textContent = "读取失败";
      app.els.updatePhase.textContent = "-";
      if (app.els.updateSource) app.els.updateSource.textContent = "-";
      if (app.els.updateWindow) app.els.updateWindow.textContent = "-";
      app.els.updateMessage.textContent = String(e.message || e);
      return null;
    }
  }

  function formatDuration(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "-";
    if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
    const minutes = Math.floor(seconds / 60);
    const remainder = Math.round(seconds % 60);
    return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
  }

  function renderUpdateHistory(records) {
    if (!app.els.updateHistory) return;
    const items = Array.isArray(records) ? records : [];
    if (!items.length) {
      app.els.updateHistory.innerHTML = `<div class="empty-list is-compact">还没有历史更新记录。</div>`;
      return;
    }
    app.els.updateHistory.innerHTML = items.map((record) => {
      const success = record.status === "success";
      const statusLabel = success ? "成功" : "失败";
      const source = record.task_source || "数据更新";
      const windowLabel = record.fetch_window_label || "未指定拉取窗口";
      const detail = record.error || record.message || (success ? "更新完成" : "更新失败");
      return `
        <article class="update-history-item ${success ? "is-success" : "is-failed"}">
          <div class="update-history-main">
            <div class="update-history-title">
              <span class="update-history-status">${statusLabel}</span>
              <strong>${list.escapeHtml(source)}</strong>
              <span>${list.escapeHtml(windowLabel)}</span>
            </div>
            <div class="update-history-meta">
              <span>开始 ${list.escapeHtml(formatDateTime(record.started_at))}</span>
              <span>结束 ${list.escapeHtml(formatDateTime(record.finished_at))}</span>
              <span>耗时 ${list.escapeHtml(formatDuration(record.elapsed_seconds))}</span>
            </div>
            <p>${list.escapeHtml(detail)}</p>
          </div>
          <div class="update-history-counts" aria-label="本次更新统计">
            <span><b>${Number(record.resources_updated || 0)}</b> 更新</span>
            <span><b>${Number(record.resources_created || 0)}</b> 新增</span>
            <span><b>${Number(record.total_new_points || 0)}</b> 数据点</span>
            <span><b>${Number(record.predicted_resources || 0)}</b> 预测</span>
          </div>
        </article>
      `;
    }).join("");
  }

  async function refreshUpdateHistory() {
    if (!app.els.updateHistory) return;
    try {
      const payload = await api.requestJson("/api/update-history?limit=20", 1);
      renderUpdateHistory(payload.records || []);
    } catch (e) {
      app.els.updateHistory.innerHTML = `<div class="empty-list is-compact">历史记录读取失败：${list.escapeHtml(e.message || e)}</div>`;
    }
  }

  function stopUpdatePolling() {
    if (updatePollTimer !== null) {
      window.clearTimeout(updatePollTimer);
      updatePollTimer = null;
    }
  }

  function startUpdatePolling() {
    stopUpdatePolling();
    updatePollWasRunning = true;
    const poll = async () => {
      const status = await refreshUpdateStatus();
      if (!status) return;
      const running = Boolean(status.running);
      if (running) {
        updatePollWasRunning = true;
        updatePollTimer = window.setTimeout(poll, 1500);
        return;
      }
      updatePollTimer = null;
      refreshUpdateHistory();
      if (updatePollWasRunning && !status.last_error) {
        loadQueue({ keepSelection: true }).catch((e) => {
          app.els.summaryText.textContent = `刷新失败：${String(e.message || e)}`;
        });
      }
    };
    updatePollTimer = window.setTimeout(poll, 600);
  }

  const VM_CLUSTER_DEFAULT = {
    cloud_type: "openstack",
    cluster: "",
    control_host: "",
    ssh_user: "root",
    ssh_port: 22,
    ssh_key: "/root/.ssh/id_rsa",
    openstack_rc: "/root/admin-openstack.sh",
    auto_confirm_resize: false,
    command_timeout_seconds: 300,
  };

  const K8S_SCALING_CLUSTER_DEFAULT = {
    cloud_type: "k8s",
    cluster: "",
    control_host: "",
    ssh_user: "root",
    ssh_port: 22,
    ssh_key: "/root/.ssh/id_rsa",
    kubeconfig: "/root/.kube/config",
    command_timeout_seconds: 300,
  };

  const K8S_PROM_DEFAULT = {
    cluster: "",
    prometheus_url: "",
    namespace_regex: "",
    bearer_token: "",
    basic_auth: "",
    rate_window: "",
  };

  function configInput(label, name, value, options = {}) {
    const type = options.type || "text";
    const disabled = options.disabled ? "disabled" : "";
    const disabledClass = options.disabled ? " is-disabled" : "";
    if (type === "checkbox") {
      return `
        <label class="config-field is-checkbox${disabledClass}">
          <span>${label}</span>
          <input data-config-name="${name}" type="checkbox" ${value ? "checked" : ""} ${disabled} />
        </label>
      `;
    }
    return `
      <label class="config-field${disabledClass}">
        <span>${label}</span>
        <input data-config-name="${name}" type="${type}" value="${list.escapeHtml(value ?? "")}" placeholder="${list.escapeHtml(options.placeholder || "")}" ${disabled} />
      </label>
    `;
  }

  function isK8sScalingCluster(cfg) {
    const type = String(cfg?.cloud_type || cfg?.type || "openstack").trim().toLowerCase();
    return type === "k8s" || type === "kubernetes";
  }

  function renderVmClusterRows(clusters) {
    if (!app.els.vmClusterList) return;
    const entries = Object.entries(clusters || {});
    if (!entries.length) {
      app.els.vmClusterList.innerHTML = `<div class="empty-list is-compact">暂无 VM 调配集群配置。</div>`;
      return;
    }
    app.els.vmClusterList.innerHTML = entries.map(([cluster, cfg]) => {
      const isK8s = isK8sScalingCluster(cfg);
      return `
      <div class="config-row" data-config-kind="vm" data-scaling-cloud-type="${isK8s ? "k8s" : "openstack"}" data-original-cluster="${list.escapeHtml(cluster)}">
        <div class="config-row-title">
          <strong>${list.escapeHtml(cluster)}</strong>
          <span class="resource-type-badge">${isK8s ? "K8S" : "OpenStack"}</span>
          <button class="link-btn" type="button" data-config-remove>删除</button>
        </div>
        <div class="config-grid">
          ${configInput("集群名", "cluster", cluster)}
          ${configInput("类型", "cloud_type", isK8s ? "k8s" : "openstack", { disabled: true })}
          ${configInput("控制节点", "control_host", cfg.control_host || "")}
          ${configInput("SSH 用户", "ssh_user", cfg.ssh_user || "root")}
          ${configInput("SSH 端口", "ssh_port", cfg.ssh_port || 22, { type: "number" })}
          ${configInput("SSH Key", "ssh_key", cfg.ssh_key || "")}
          ${configInput("OpenStack RC", "openstack_rc", cfg.openstack_rc || "", { disabled: isK8s })}
          ${configInput("Kubeconfig", "kubeconfig", cfg.kubeconfig || "", { placeholder: "/root/.kube/config", disabled: !isK8s })}
          ${configInput("命令超时秒", "command_timeout_seconds", cfg.command_timeout_seconds || 300, { type: "number" })}
          ${configInput("自动确认 resize", "auto_confirm_resize", Boolean(cfg.auto_confirm_resize), { type: "checkbox", disabled: isK8s })}
        </div>
      </div>
    `;
    }).join("");
  }

  function renderK8sClusterRows(clusters) {
    if (!app.els.k8sClusterList) return;
    const items = Array.isArray(clusters) ? clusters : [];
    if (!items.length) {
      app.els.k8sClusterList.innerHTML = `<div class="empty-list is-compact">暂无 K8S Prometheus 接入配置。</div>`;
      return;
    }
    app.els.k8sClusterList.innerHTML = items.map((cfg) => `
      <div class="config-row" data-config-kind="k8s" data-original-cluster="${list.escapeHtml(cfg.cluster || "")}">
        <div class="config-row-title">
          <strong>${list.escapeHtml(cfg.cluster || "未命名接入")}</strong>
          <button class="link-btn" type="button" data-config-remove>删除</button>
          <button class="link-btn" type="button" data-k8s-fetch-single>拉取</button>
        </div>
        <div class="config-grid">
          ${configInput("集群名", "cluster", cfg.cluster || "")}
          ${configInput("Prometheus URL", "prometheus_url", cfg.prometheus_url || "", { placeholder: "http://127.0.0.1:9090" })}
          ${configInput("Namespace 正则", "namespace_regex", cfg.namespace_regex || "", { placeholder: "prod|default" })}
          ${configInput("Bearer Token", "bearer_token", cfg.bearer_token || "")}
          ${configInput("Basic Auth", "basic_auth", cfg.basic_auth || "", { placeholder: "base64(user:password)" })}
          ${configInput("Rate 窗口", "rate_window", cfg.rate_window || "", { placeholder: "5m" })}
        </div>
      </div>
    `).join("");
  }

  function renderForecastModelRows(payload) {
    if (!app.els.forecastModelList) return;
    const supported = Array.isArray(payload?.supported_methods) ? payload.supported_methods : [];
    const enabled = new Set(payload?.enabled_methods || []);
    const ensembleEnabled = Boolean(payload?.enable_ensemble);
    const reuseEnabled = payload?.reuse_backtest_model_for_future !== false;
    const prophetRoutingEnabled = payload?.prophet_routing_enabled !== false;
    const prophetRoutingMode = payload?.prophet_routing_mode || "auto";
    app.els.forecastModelList.innerHTML = `
      <div class="config-row" data-config-kind="forecast">
        <div class="config-row-title">
          <strong>候选模型</strong>
        </div>
        <div class="config-grid">
          ${supported.map((method) => configInput(
            method.label || method.key,
            `method:${method.key}`,
            enabled.has(method.key),
            { type: "checkbox" }
          )).join("")}
          ${configInput("Ensemble", "enable_ensemble", ensembleEnabled, { type: "checkbox" })}
          ${configInput("多段预测复用", "reuse_backtest_model_for_future", reuseEnabled, { type: "checkbox" })}
          ${configInput("Prophet 智能路由", "prophet_routing_enabled", prophetRoutingEnabled, { type: "checkbox" })}
          ${configInput("Prophet 路由模式", "prophet_routing_mode", prophetRoutingMode, { placeholder: "auto" })}
        </div>
      </div>
    `;
  }

  function setConfigMessage(message, isError = false) {
    if (!app.els.clusterConfigMessage) return;
    app.els.clusterConfigMessage.textContent = message || "";
    app.els.clusterConfigMessage.classList.toggle("is-error", Boolean(isError));
  }

  function describeUpdateStatus(status) {
    if (!status || typeof status !== "object") return "";
    const phase = status.phase || status.step || "-";
    const message = status.message || status.last_error || status.error || "";
    const source = status.task_source || "";
    const windowLabel = status.fetch_window_label || "";
    const started = formatDateTime(status.last_started_at || status.started_at || status.start_time);
    const parts = [`当前阶段：${phase}`];
    if (source) parts.push(`任务来源：${source}`);
    if (windowLabel) parts.push(`拉取窗口：${windowLabel}`);
    if (message) parts.push(`状态消息：${message}`);
    if (started && started !== "-") parts.push(`开始时间：${started}`);
    return parts.join("\n");
  }

  function renderK8sScheduleHint(schedule) {
    if (!app.els.k8sScheduleHint) return;
    const interval = Number(schedule?.scheduled_update_interval_minutes || 0);
    const overlap = Number(schedule?.incremental_overlap_minutes || 0);
    const historyDays = Number(schedule?.history_days || 7);
    const incrementalHours = Math.max(1, (interval + overlap) / 60);
    const incrementalLabel = Number.isInteger(incrementalHours)
      ? String(incrementalHours)
      : incrementalHours.toFixed(1);
    app.els.k8sScheduleHint.textContent =
      `app.py 启动后不会自动拉取 K8S 数据，可通过页面按钮手动触发；有本地基线时拉取最近 ${incrementalLabel} 小时，` +
      `无本地基线或全量刷新时拉取最近 ${historyDays} 天。`;
  }

  async function refreshClusterConfigs() {
    if (!app.els.vmClusterList || !app.els.k8sClusterList) return;
    app.els.vmClusterList.innerHTML = `<div class="empty-list is-compact">正在读取 VM 调配集群...</div>`;
    app.els.k8sClusterList.innerHTML = `<div class="empty-list is-compact">正在读取 K8S 监控接入...</div>`;
    if (app.els.forecastModelList) {
      app.els.forecastModelList.innerHTML = `<div class="empty-list is-compact">正在读取预测模型配置...</div>`;
    }
    try {
      const [payload, forecastPayload] = await Promise.all([
        api.requestJson("/api/cluster-configs", 1),
        api.requestJson("/api/forecast-config", 1),
      ]);
      app.state.clusterConfigPayload = payload;
      app.state.forecastConfigPayload = forecastPayload;
      renderVmClusterRows(payload.vm_scaling_clusters || {});
      renderK8sScheduleHint(payload.k8s_prometheus_schedule || {});
      renderK8sClusterRows(payload.k8s_prometheus_clusters || []);
      renderForecastModelRows(forecastPayload);
      setConfigMessage("配置已加载。");
    } catch (e) {
      const msg = String(e.message || e);
      app.els.vmClusterList.innerHTML = `<div class="empty-list is-compact">读取失败：${list.escapeHtml(msg)}</div>`;
      app.els.k8sClusterList.innerHTML = "";
      if (app.els.k8sScheduleHint) app.els.k8sScheduleHint.textContent = "";
      if (app.els.forecastModelList) app.els.forecastModelList.innerHTML = "";
      setConfigMessage(msg, true);
    }
  }

  function rowValue(row, name) {
    const input = row.querySelector(`[data-config-name="${name}"]`);
    if (!input) return "";
    if (input.type === "checkbox") return input.checked;
    if (input.type === "number") return Number(input.value || 0);
    return input.value.trim();
  }

  function collectClusterConfigs() {
    const vm = {};
    app.els.vmClusterList?.querySelectorAll('[data-config-kind="vm"]').forEach((row) => {
      const cluster = rowValue(row, "cluster");
      if (!cluster) return;
      const originalCluster = row.dataset.originalCluster || cluster;
      const original = app.state.clusterConfigPayload?.vm_scaling_clusters?.[originalCluster] || {};
      const cloudType = row.dataset.scalingCloudType || rowValue(row, "cloud_type") || "openstack";
      const isK8s = cloudType === "k8s";
      vm[cluster] = {
        ...original,
        cloud_type: isK8s ? "k8s" : "openstack",
        control_host: rowValue(row, "control_host"),
        ssh_user: rowValue(row, "ssh_user"),
        ssh_port: rowValue(row, "ssh_port") || 22,
        ssh_key: rowValue(row, "ssh_key"),
        command_timeout_seconds: rowValue(row, "command_timeout_seconds") || 300,
      };
      if (isK8s) {
        vm[cluster].kubeconfig = rowValue(row, "kubeconfig");
        delete vm[cluster].openstack_rc;
        delete vm[cluster].auto_confirm_resize;
      } else {
        vm[cluster].openstack_rc = rowValue(row, "openstack_rc");
        vm[cluster].auto_confirm_resize = rowValue(row, "auto_confirm_resize");
        delete vm[cluster].kubeconfig;
      }
    });
    const k8s = [];
    app.els.k8sClusterList?.querySelectorAll('[data-config-kind="k8s"]').forEach((row) => {
      const cluster = rowValue(row, "cluster");
      const prometheusUrl = rowValue(row, "prometheus_url");
      if (!cluster && !prometheusUrl) return;
      const originalCluster = row.dataset.originalCluster || cluster;
      const original = (app.state.clusterConfigPayload?.k8s_prometheus_clusters || [])
        .find((item) => item.cluster === originalCluster) || {};
      k8s.push({
        ...original,
        cluster,
        prometheus_url: prometheusUrl,
        namespace_regex: rowValue(row, "namespace_regex"),
        bearer_token: rowValue(row, "bearer_token"),
        basic_auth: rowValue(row, "basic_auth"),
        rate_window: rowValue(row, "rate_window"),
      });
    });
    return { vm_scaling_clusters: vm, k8s_prometheus_clusters: k8s };
  }

  function collectForecastConfig() {
    const enabledMethods = [];
    app.els.forecastModelList?.querySelectorAll('[data-config-name^="method:"]').forEach((input) => {
      if (input.checked) enabledMethods.push(input.dataset.configName.replace("method:", ""));
    });
    const ensembleInput = app.els.forecastModelList?.querySelector('[data-config-name="enable_ensemble"]');
    const reuseInput = app.els.forecastModelList?.querySelector('[data-config-name="reuse_backtest_model_for_future"]');
    const prophetRoutingInput = app.els.forecastModelList?.querySelector('[data-config-name="prophet_routing_enabled"]');
    const prophetRoutingModeInput = app.els.forecastModelList?.querySelector('[data-config-name="prophet_routing_mode"]');
    return {
      enabled_methods: enabledMethods,
      enable_ensemble: Boolean(ensembleInput?.checked),
      reuse_backtest_model_for_future: reuseInput ? Boolean(reuseInput.checked) : true,
      prophet_routing_enabled: prophetRoutingInput ? Boolean(prophetRoutingInput.checked) : true,
      prophet_routing_mode: prophetRoutingModeInput?.value?.trim() || "auto",
    };
  }

  async function saveClusterConfigs() {
    setConfigMessage("正在保存配置...");
    try {
      const [payload, forecastPayload] = await Promise.all([
        api.postJson("/api/cluster-configs", collectClusterConfigs(), "PUT"),
        api.postJson("/api/forecast-config", collectForecastConfig(), "PUT"),
      ]);
      app.state.clusterConfigPayload = payload;
      app.state.forecastConfigPayload = forecastPayload;
      renderVmClusterRows(payload.vm_scaling_clusters || {});
      renderK8sClusterRows(payload.k8s_prometheus_clusters || []);
      renderForecastModelRows(forecastPayload);
      setConfigMessage("配置已保存。后续 VM 调配、K8S 数据更新和重新预测会读取这些配置。");
    } catch (e) {
      setConfigMessage(String(e.message || e), true);
    }
  }

  async function diagnoseK8sConfigs() {
    setConfigMessage("正在诊断 K8S Prometheus 接入...");
    try {
      const names = collectClusterConfigs().k8s_prometheus_clusters.map((item) => item.cluster).filter(Boolean);
      const report = await api.postJson("/api/cluster-configs/k8s-diagnose", { clusters: names });
      setConfigMessage(JSON.stringify(report, null, 2), !report.ok);
    } catch (e) {
      setConfigMessage(String(e.message || e), true);
    }
  }

  async function fetchK8sPrometheusData(clusterNames) {
    const names = Array.isArray(clusterNames)
      ? clusterNames
      : collectClusterConfigs().k8s_prometheus_clusters.map((item) => item.cluster).filter(Boolean);
    const label = names.length === 1 ? names[0] : `${names.length} 个集群`;
    setConfigMessage(`正在提交 ${label} 的 K8S 数据拉取任务...`);
    try {
      const payload = await api.postJson("/api/cluster-configs/k8s-fetch", { clusters: names });
      setConfigMessage(payload.message || `${label} K8S 数据拉取任务已提交。`);
      setView("updates");
      startUpdatePolling();
    } catch (e) {
      if (e.status === 409 && e.updateStatus) {
        const detail = describeUpdateStatus(e.updateStatus);
        setConfigMessage(`${String(e.message || e)}\n${detail}`.trim(), true);
        updateStatusText(e.updateStatus);
        setView("updates");
        startUpdatePolling();
        return;
      }
      setConfigMessage(String(e.message || e), true);
    }
  }

  function addVmClusterRow() {
    const current = collectClusterConfigs().vm_scaling_clusters;
    let idx = Object.keys(current).length + 1;
    while (current[`cluster-openstack-${idx}`]) idx += 1;
    current[`cluster-openstack-${idx}`] = { ...VM_CLUSTER_DEFAULT };
    renderVmClusterRows(current);
  }

  function addK8sScalingClusterRow() {
    const current = collectClusterConfigs().vm_scaling_clusters;
    let idx = Object.keys(current).length + 1;
    while (current[`cluster-k8s-${idx}`]) idx += 1;
    current[`cluster-k8s-${idx}`] = { ...K8S_SCALING_CLUSTER_DEFAULT };
    renderVmClusterRows(current);
  }

  function addK8sClusterRow() {
    const current = collectClusterConfigs().k8s_prometheus_clusters;
    let idx = current.length + 1;
    while (current.some((item) => item.cluster === `cluster-k8s-${idx}`)) idx += 1;
    current.push({ ...K8S_PROM_DEFAULT, cluster: `cluster-k8s-${idx}` });
    renderK8sClusterRows(current);
  }

  function bindClusterConfigEvents() {
    app.els.clusterConfigSave?.addEventListener("click", saveClusterConfigs);
    app.els.vmClusterAdd?.addEventListener("click", addVmClusterRow);
    app.els.k8sScalingClusterAdd?.addEventListener("click", addK8sScalingClusterRow);
    app.els.k8sClusterAdd?.addEventListener("click", addK8sClusterRow);
    app.els.k8sDiagnose?.addEventListener("click", diagnoseK8sConfigs);
    app.els.k8sFetch?.addEventListener("click", () => fetchK8sPrometheusData());
    [app.els.vmClusterList, app.els.k8sClusterList].forEach((root) => {
      root?.addEventListener("click", (event) => {
        const remove = event.target.closest("[data-config-remove]");
        if (remove) {
          remove.closest(".config-row")?.remove();
          setConfigMessage("配置已在页面移除，保存后生效。");
          return;
        }
        const fetchSingle = event.target.closest("[data-k8s-fetch-single]");
        if (fetchSingle) {
          const row = fetchSingle.closest("[data-config-kind='k8s']");
          const cluster = rowValue(row, "cluster");
          if (!cluster) {
            setConfigMessage("请先填写集群名并保存配置。", true);
            return;
          }
          fetchK8sPrometheusData([cluster]);
        }
      });
    });
  }

  function bindFilters() {
    document.querySelectorAll("[data-filter-group]").forEach((group) => {
      group.addEventListener("click", (event) => {
        const btn = event.target.closest("button[data-value]");
        if (!btn) return;
        event.stopPropagation();
        const value = btn.dataset.value || "";
        const filter = group.dataset.filterGroup;
        app.state.page = 1;
        if (filter === "resource-type") {
          app.state.resourceTypeFilter = value;
          loadQueue();
        } else if (filter === "confidence") {
          applyConfidenceFilter(value);
        } else {
          applyActionFilter(value, { toggle: true });
        }
      });
    });
    app.els.summaryItems.forEach((item) => {
      item.addEventListener("click", (event) => {
        event.stopPropagation();
        applyActionFilter(item.dataset.summaryAction || "", { toggle: true });
      });
    });
  }

  function bindEvents() {
    app.els.navTabs.forEach((tab) => {
      tab.addEventListener("click", () => setView(tab.dataset.view || "risk"));
    });
    app.els.searchBtn.addEventListener("click", () => {
      app.state.query = app.els.searchInput.value.trim();
      app.state.page = 1;
      loadQueue();
    });
    app.els.searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        app.state.query = app.els.searchInput.value.trim();
        app.state.page = 1;
        loadQueue();
      }
    });
    app.els.resetBtn.addEventListener("click", resetFilters);
    app.els.refreshBtn.addEventListener("click", () => loadQueue({ keepSelection: true }));
    app.els.prevPageBtn.addEventListener("click", () => {
      if (app.els.prevPageBtn.disabled) return;
      app.state.page -= 1;
      loadQueue({ keepSelection: true });
    });
    app.els.nextPageBtn.addEventListener("click", () => {
      if (app.els.nextPageBtn.disabled) return;
      app.state.page += 1;
      loadQueue({ keepSelection: true });
    });
    app.els.rowsRoot.addEventListener("click", (event) => {
      const row = event.target.closest(".risk-row");
      if (!row) return;
      list.selectResource(row.dataset.resourceId || "");
    });
    app.els.metricTabs.addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-metric-key]");
      if (!btn || !app.state.selectedResourceId) return;
      app.state.selectedMetricKey = btn.dataset.metricKey || "cpu";
      list.selectResource(app.state.selectedResourceId, app.state.selectedMetricKey);
    });
    app.els.detailTabs.forEach((tab) => {
      tab.addEventListener("click", () => setDetailTab(tab.dataset.detailTab || "summary"));
    });
    app.els.chartGuideBtn.addEventListener("click", charts.toggleChartAuxiliary);
    app.els.chartZoomBtn?.addEventListener("click", () => charts.openChartModal());
    app.els.detailClose.addEventListener("click", () => {
      app.els.detailPanel.classList.remove("is-open");
    });
    app.els.mobileFilterOpen.addEventListener("click", () => app.els.filterPanel.classList.add("is-open"));
    app.els.mobileFilterClose.addEventListener("click", () => app.els.filterPanel.classList.remove("is-open"));
    window.addEventListener("resource-selected", () => {
      if (app.state.activeView === "tasks") renderTaskPanel();
    });
    app.els.detailPanel.addEventListener("click", (event) => {
      const scaleBtn = event.target.closest("[data-scaling-mode]");
      if (!scaleBtn) return;
      event.preventDefault();
      if (scaleBtn.disabled) return;
      window.ScalingUI.start(scaleBtn, { requestJson: api.requestJson, postJson: api.postJson });
    });
    bindClusterConfigEvents();
    window.addEventListener("resource-scaled", () => loadQueue({ keepSelection: true }).catch((e) => {
      app.els.summaryText.textContent = `刷新失败：${String(e.message || e)}`;
    }));
    window.addEventListener("resource-scaled", () => {
      if (app.state.activeView === "tasks") renderTaskPanel();
    });
  }

  async function bootstrap() {
    if (typeof echarts === "undefined") {
      app.els.rowsRoot.innerHTML = `<div class="empty-list">ECharts 未加载，请确认 static/vendor/echarts/echarts.min.js 存在。</div>`;
      return;
    }
    bindFilters();
    bindEvents();
    setDetailTab("summary");
    setView("risk");
    try {
      await loadQueue();
    } catch (e) {
      app.els.summaryText.textContent = `加载失败：${String(e.message || e)}`;
      app.els.rowsRoot.innerHTML = `<div class="empty-list">资源列表加载失败。</div>`;
    }
  }

  bootstrap();
})();
