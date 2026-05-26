(function () {
  const app = window.ResourcePredictApp;
  const api = window.ResourceApi;
  const charts = window.ResourceCharts;
  const list = window.ResourceList;

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
    if (app.state.activeView === "updates") refreshUpdateStatus();
    if (app.state.activeView === "configs") refreshClusterConfigs();
  }

  function buildActionForApi() {
    return "";
  }

  async function loadQueue({ keepSelection = false } = {}) {
    app.els.summaryText.textContent = "正在加载资源...";
    const payload = await api.requestJson(api.buildQuery("/api/resources", {
      page: 1,
      page_size: app.API_PAGE_SIZE,
      sort_by: "urgency_score",
      resource_type: app.state.resourceTypeFilter,
      action: buildActionForApi(),
      q: app.state.query,
    }));
    const items = payload.items || [];
    if (!keepSelection) app.state.selectedResourceId = "";
    list.setItems(items);
    list.syncFilterButtons();
  }

  function applyActionFilter(value, options = {}) {
    const nextValue = value || "";
    app.state.actionFilter = options.toggle && app.state.actionFilter === nextValue ? "" : nextValue;
    app.state.page = 1;
    app.state.visibleItems = list.applyClientFilters(app.state.loadedItems);
    list.renderRows();
    list.syncFilterButtons();
  }

  function applyConfidenceFilter(value) {
    app.state.confidenceFilter = value || "";
    app.state.page = 1;
    app.state.visibleItems = list.applyClientFilters(app.state.loadedItems);
    list.renderRows();
    list.syncFilterButtons();
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
    const d = new Date(value);
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

  function renderTaskHistory(tasks) {
    if (!app.els.taskHistory) return;
    if (!tasks.length) {
      app.els.taskHistory.innerHTML = `<div class="empty-list is-compact">当前资源还没有调配记录。</div>`;
      return;
    }
    app.els.taskHistory.innerHTML = tasks.map((task) => `
      <div class="task-item">
        <div>
          <strong>${list.escapeHtml(task.mode || task.action || "调配任务")}</strong>
          <span>${list.escapeHtml(task.task_id || "")}</span>
        </div>
        <div>
          <span class="task-status">${list.escapeHtml(task.status || "-")}</span>
          <small>${list.escapeHtml(formatDateTime(task.created_at || task.updated_at || task.finished_at))}</small>
        </div>
      </div>
    `).join("");
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
    if (list.isK8s(resource)) {
      app.els.taskCapability.textContent = "仅分析";
      app.els.taskHistory.innerHTML = `<div class="analysis-only">K8S Workload 当前仅提供分析建议，不执行自动调配。</div>`;
      return;
    }
    app.els.taskCapability.textContent = "可预检 / 可调配";
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
    app.els.updateStarted.textContent = formatDateTime(status?.started_at || status?.start_time);
    app.els.updateFinished.textContent = formatDateTime(status?.finished_at || status?.end_time || status?.last_success_at);
    const message = status?.message || status?.error || status?.last_error || "";
    app.els.updateMessage.textContent = message ? String(message) : "暂无更新消息。";
  }

  async function refreshUpdateStatus() {
    if (!app.els.updateRunning) return;
    app.els.updateRunning.textContent = "读取中";
    try {
      const status = await api.requestJson("/api/update-status", 1);
      updateStatusText(status);
    } catch (e) {
      app.els.updateRunning.textContent = "读取失败";
      app.els.updatePhase.textContent = "-";
      app.els.updateMessage.textContent = String(e.message || e);
    }
  }

  const VM_CLUSTER_DEFAULT = {
    cloud_type: "openstack",
    cluster: "",
    control_host: "",
    ssh_user: "root",
    ssh_port: 22,
    ssh_key: "",
    openstack_rc: "/root/admin-openrc",
    auto_confirm_resize: false,
    command_timeout_seconds: 300,
  };

  const K8S_PROM_DEFAULT = {
    cluster: "",
    prometheus_url: "",
    namespace_regex: "",
    bearer_token: "",
    basic_auth: "",
  };

  function configInput(label, name, value, options = {}) {
    const type = options.type || "text";
    if (type === "checkbox") {
      return `
        <label class="config-field is-checkbox">
          <span>${label}</span>
          <input data-config-name="${name}" type="checkbox" ${value ? "checked" : ""} />
        </label>
      `;
    }
    return `
      <label class="config-field">
        <span>${label}</span>
        <input data-config-name="${name}" type="${type}" value="${list.escapeHtml(value ?? "")}" placeholder="${list.escapeHtml(options.placeholder || "")}" />
      </label>
    `;
  }

  function renderVmClusterRows(clusters) {
    if (!app.els.vmClusterList) return;
    const entries = Object.entries(clusters || {});
    if (!entries.length) {
      app.els.vmClusterList.innerHTML = `<div class="empty-list is-compact">暂无 VM 调配集群配置。</div>`;
      return;
    }
    app.els.vmClusterList.innerHTML = entries.map(([cluster, cfg]) => `
      <div class="config-row" data-config-kind="vm" data-original-cluster="${list.escapeHtml(cluster)}">
        <div class="config-row-title">
          <strong>${list.escapeHtml(cluster)}</strong>
          <button class="link-btn" type="button" data-config-remove>删除</button>
        </div>
        <div class="config-grid">
          ${configInput("集群名", "cluster", cluster)}
          ${configInput("类型", "cloud_type", cfg.cloud_type || "openstack")}
          ${configInput("控制节点", "control_host", cfg.control_host || "")}
          ${configInput("SSH 用户", "ssh_user", cfg.ssh_user || "root")}
          ${configInput("SSH 端口", "ssh_port", cfg.ssh_port || 22, { type: "number" })}
          ${configInput("SSH Key", "ssh_key", cfg.ssh_key || "")}
          ${configInput("OpenStack RC", "openstack_rc", cfg.openstack_rc || "")}
          ${configInput("命令超时秒", "command_timeout_seconds", cfg.command_timeout_seconds || 300, { type: "number" })}
          ${configInput("自动确认 resize", "auto_confirm_resize", Boolean(cfg.auto_confirm_resize), { type: "checkbox" })}
        </div>
      </div>
    `).join("");
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
        </div>
        <div class="config-grid">
          ${configInput("集群名", "cluster", cfg.cluster || "")}
          ${configInput("Prometheus URL", "prometheus_url", cfg.prometheus_url || "", { placeholder: "http://127.0.0.1:9090" })}
          ${configInput("Namespace 正则", "namespace_regex", cfg.namespace_regex || "", { placeholder: "prod|default" })}
          ${configInput("Bearer Token", "bearer_token", cfg.bearer_token || "")}
          ${configInput("Basic Auth", "basic_auth", cfg.basic_auth || "", { placeholder: "base64(user:password)" })}
        </div>
      </div>
    `).join("");
  }

  function setConfigMessage(message, isError = false) {
    if (!app.els.clusterConfigMessage) return;
    app.els.clusterConfigMessage.textContent = message || "";
    app.els.clusterConfigMessage.classList.toggle("is-error", Boolean(isError));
  }

  async function refreshClusterConfigs() {
    if (!app.els.vmClusterList || !app.els.k8sClusterList) return;
    app.els.vmClusterList.innerHTML = `<div class="empty-list is-compact">正在读取 VM 调配集群...</div>`;
    app.els.k8sClusterList.innerHTML = `<div class="empty-list is-compact">正在读取 K8S 监控接入...</div>`;
    try {
      const payload = await api.requestJson("/api/cluster-configs", 1);
      app.state.clusterConfigPayload = payload;
      renderVmClusterRows(payload.vm_scaling_clusters || {});
      renderK8sClusterRows(payload.k8s_prometheus_clusters || []);
      setConfigMessage("配置已加载。");
    } catch (e) {
      const msg = String(e.message || e);
      app.els.vmClusterList.innerHTML = `<div class="empty-list is-compact">读取失败：${list.escapeHtml(msg)}</div>`;
      app.els.k8sClusterList.innerHTML = "";
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
      vm[cluster] = {
        ...original,
        cloud_type: rowValue(row, "cloud_type") || "openstack",
        control_host: rowValue(row, "control_host"),
        ssh_user: rowValue(row, "ssh_user"),
        ssh_port: rowValue(row, "ssh_port") || 22,
        ssh_key: rowValue(row, "ssh_key"),
        openstack_rc: rowValue(row, "openstack_rc"),
        command_timeout_seconds: rowValue(row, "command_timeout_seconds") || 300,
        auto_confirm_resize: rowValue(row, "auto_confirm_resize"),
      };
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
      });
    });
    return { vm_scaling_clusters: vm, k8s_prometheus_clusters: k8s };
  }

  async function saveClusterConfigs() {
    setConfigMessage("正在保存配置...");
    try {
      const payload = await api.postJson("/api/cluster-configs", collectClusterConfigs(), "PUT");
      app.state.clusterConfigPayload = payload;
      renderVmClusterRows(payload.vm_scaling_clusters || {});
      renderK8sClusterRows(payload.k8s_prometheus_clusters || []);
      setConfigMessage("配置已保存。后续 VM 调配和 K8S 数据更新会读取这些配置。");
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

  async function fetchK8sPrometheusData() {
    setConfigMessage("正在提交 K8S 数据拉取任务...");
    try {
      const names = collectClusterConfigs().k8s_prometheus_clusters.map((item) => item.cluster).filter(Boolean);
      const payload = await api.postJson("/api/cluster-configs/k8s-fetch", { clusters: names });
      setConfigMessage(payload.message || "K8S 数据拉取任务已提交。");
      setView("updates");
      setTimeout(() => refreshUpdateStatus(), 600);
    } catch (e) {
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
    app.els.k8sClusterAdd?.addEventListener("click", addK8sClusterRow);
    app.els.k8sDiagnose?.addEventListener("click", diagnoseK8sConfigs);
    app.els.k8sFetch?.addEventListener("click", fetchK8sPrometheusData);
    [app.els.vmClusterList, app.els.k8sClusterList].forEach((root) => {
      root?.addEventListener("click", (event) => {
        const remove = event.target.closest("[data-config-remove]");
        if (!remove) return;
        remove.closest(".config-row")?.remove();
        setConfigMessage("配置已在页面移除，保存后生效。");
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
      list.renderRows();
    });
    app.els.nextPageBtn.addEventListener("click", () => {
      if (app.els.nextPageBtn.disabled) return;
      app.state.page += 1;
      list.renderRows();
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
