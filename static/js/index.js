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
