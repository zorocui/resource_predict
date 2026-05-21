(function () {
  const app = window.ResourcePredictApp;
  const api = window.ResourceApi;
  const charts = window.ResourceCharts;
  const resourceList = window.ResourceList;

  function syncActionCardActive() {
    for (const card of app.actionCards) {
      const action = card.dataset.actionFilter || "";
      card.classList.toggle("active", !!action && action === app.state.actionFilter);
    }
  }

  function updatePager() {
    if (app.state.mode === "topn") {
      app.prevPageBtn.disabled = true;
      app.nextPageBtn.disabled = app.state.total <= app.TOP_N_DEFAULT;
      const totalPages = Math.max(1, Math.ceil((app.state.total || 0) / app.state.pageSize));
      app.pagerText.textContent = `Top${app.TOP_N_DEFAULT} 模式 / 共 ${totalPages} 页`;
      return;
    }
    const totalPages = Math.max(1, Math.ceil((app.state.total || 0) / app.state.pageSize));
    app.prevPageBtn.disabled = app.state.page <= 1;
    app.nextPageBtn.disabled = app.state.page >= totalPages;
    app.pagerText.textContent = `第 ${app.state.page} / ${totalPages} 页`;
  }

  function renderFromPayload(payload, summaryPrefix) {
    const items = payload.items || [];
    app.state.total = Number(payload.total || 0);
    resourceList.renderRows(items);
    app.summaryText.textContent = `${summaryPrefix}，共匹配 ${app.state.total} 条，当前展示 ${items.length} 条`;
    charts.bindLazyLoad();
    charts.prefetchResourceDetails(items);
    updatePager();
  }

  async function loadTopN() {
    app.state.mode = "topn";
    app.state.page = 1;
    app.state.q = "";
    const payload = await api.requestJson(api.buildQuery("/api/resources", {
      top_n: app.TOP_N_DEFAULT,
      sort_by: "urgency_score",
      action: app.state.actionFilter,
    }));
    renderFromPayload(payload, `默认展示 Top${app.TOP_N_DEFAULT}`);
    await resourceList.refreshAdviceSummary();
    syncActionCardActive();
  }

  async function loadPage(page) {
    app.state.mode = "list";
    app.state.page = Math.max(1, page);
    app.state.q = "";
    const payload = await api.requestJson(api.buildQuery("/api/resources", {
      page: app.state.page,
      page_size: app.state.pageSize,
      sort_by: "urgency_score",
      action: app.state.actionFilter,
    }));
    renderFromPayload(payload, "全部资源分页浏览");
    await resourceList.refreshAdviceSummary();
    syncActionCardActive();
  }

  async function loadSearchPage(page) {
    app.state.mode = "search";
    app.state.page = Math.max(1, page);
    const payload = await api.requestJson(api.buildQuery("/api/resources", {
      q: app.state.q,
      page: app.state.page,
      page_size: app.state.pageSize,
      sort_by: "urgency_score",
      action: app.state.actionFilter,
    }));
    renderFromPayload(payload, `搜索 "${app.state.q}"`);
    await resourceList.refreshAdviceSummary();
    syncActionCardActive();
  }

  async function gotoPage(page) {
    const targetPage = Math.max(1, Number(page) || 1);
    if (app.state.mode === "search") {
      await loadSearchPage(targetPage);
      return;
    }
    await loadPage(targetPage);
  }

  async function reloadCurrentView() {
    if (app.state.mode === "topn") {
      await loadTopN();
      return;
    }
    if (app.state.mode === "search") {
      await loadSearchPage(app.state.page || 1);
      return;
    }
    await loadPage(app.state.page || 1);
  }

  async function searchResources() {
    const q = (app.searchInput.value || "").trim();
    if (!q) {
      await loadTopN();
      return;
    }
    app.state.q = q;
    await loadSearchPage(1);
  }

  function syncChartGuideButton() {
    if (!app.chartGuideBtn) return;
    app.chartGuideBtn.classList.toggle("is-active", app.chartAuxiliaryVisible);
    app.chartGuideBtn.setAttribute("aria-pressed", app.chartAuxiliaryVisible ? "true" : "false");
    app.chartGuideBtn.textContent = app.chartAuxiliaryVisible ? "辅助线：开" : "辅助线：关";
  }

  function toggleChartAuxiliary() {
    charts.toggleChartAuxiliary();
    syncChartGuideButton();
  }

  function bindEvents() {
    app.searchBtn?.addEventListener("click", () => searchResources());
    app.resetBtn?.addEventListener("click", () => {
      app.searchInput.value = "";
      loadPage(1);
    });
    app.browseBtn?.addEventListener("click", () => loadPage(1));
    app.topnBtn?.addEventListener("click", () => loadTopN());
    app.chartGuideBtn?.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleChartAuxiliary();
    });
    app.prevPageBtn?.addEventListener("click", (e) => {
      e.preventDefault();
      if (!app.prevPageBtn.disabled) gotoPage(app.state.page - 1);
    });
    app.nextPageBtn?.addEventListener("click", (e) => {
      e.preventDefault();
      if (!app.nextPageBtn.disabled) gotoPage(app.state.page + 1);
    });
    for (const card of app.actionCards) {
      card.addEventListener("click", () => {
        const action = card.dataset.actionFilter || "";
        app.state.actionFilter = app.state.actionFilter === action ? "" : action;
        gotoPage(1);
      });
    }
    app.searchInput?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") searchResources();
    });
    app.rowsRoot.addEventListener("click", (e) => {
      const scaleBtn = e.target.closest("[data-scaling-mode]");
      if (scaleBtn) {
        e.preventDefault();
        if (scaleBtn.disabled) return;
        window.ScalingUI.start(scaleBtn, { requestJson: api.requestJson, postJson: api.postJson });
        return;
      }
      const mobileTab = e.target.closest(".mobile-metric-tab");
      if (mobileTab) {
        const row = mobileTab.closest(".row");
        const metricKey = mobileTab.dataset.mobileMetric;
        if (!row || !metricKey) return;
        row.querySelectorAll(".mobile-metric-tab").forEach((tab) => {
          tab.classList.toggle("active", tab === mobileTab);
        });
        row.querySelectorAll(".img-wrap").forEach((wrap) => {
          const chart = wrap.querySelector(".chart");
          const isTarget = chart?.dataset.metricKey === metricKey;
          wrap.classList.toggle("mobile-hidden", !isTarget);
          if (isTarget && chart) {
            charts.fetchAndRenderDetail(chart.dataset.resourceId, metricKey, chart);
          }
        });
        return;
      }
      const btn = e.target.closest(".chart-expand-btn");
      if (!btn) return;
      const wrap = btn.closest(".img-wrap");
      const chartDom = wrap?.querySelector(".chart");
      const resourceId = chartDom?.dataset.resourceId;
      const metricKey = chartDom?.dataset.metricKey;
      if (!resourceId || !metricKey) return;
      e.preventDefault();
      charts.openChartModal(resourceId, metricKey);
    });
    app.chartModalEl?.addEventListener("click", (e) => {
      if (e.target.closest("[data-chart-modal-dismiss]")) charts.closeChartModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && app.chartModalEl && !app.chartModalEl.hidden) charts.closeChartModal();
    });
    window.addEventListener("resize", () => {
      for (const chart of app.chartInstances.values()) chart.resize();
      if (app.modalChartInstance) app.modalChartInstance.resize();
    });
    window.addEventListener("resource-scaled", () => {
      reloadCurrentView().catch((e) => {
        app.summaryText.textContent = `刷新失败：${String(e)}`;
      });
    });
  }

  async function bootstrap() {
    if (charts.showEchartsError()) return;
    syncChartGuideButton();
    bindEvents();
    try {
      await loadPage(1);
    } catch (e) {
      app.summaryText.textContent = `加载失败：${String(e)}`;
    }
  }

  bootstrap();
})();
