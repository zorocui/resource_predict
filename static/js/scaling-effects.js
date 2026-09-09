(function () {
  const labels = { scale_in: "缩容", scale_out: "扩容", mixed: "混合调整", unknown: "方向待核验", openstack_vm: "VM", k8s_workload: "K8S Workload", cpu: "CPU", memory: "内存", capacity: "实际容量", request: "Request", limit: "Limit", executing: "执行中", awaiting_effective: "等待实测生效", observing: "观察中", insufficient_data: "样本不足", evaluated: "已评估", failed: "执行失败", interrupted: "后续调配已截断", basis_changed: "容量口径漂移", missing_baseline: "缺少基线", capture_failed: "证据留档失败", evidence_conflict: "证据冲突", expired: "证据补齐已到期" };
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const label = value => labels[value] || value || "—";
  const number = (value, suffix = "") => value == null || value === "" || !Number.isFinite(Number(value)) ? "—" : `${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${suffix}`;
  const date = value => value == null || !Number.isFinite(Number(value)) ? "—" : new Date(Number(value)).toLocaleString("zh-CN", { hour12: false });
  const pct = value => number(value, "%");
  const coverage = value => value == null ? "—" : pct(value * 100);
  const metricName = m => [label(m.resource_type), m.container ? `容器 ${m.container}` : "资源汇总", label(m.metric), label(m.basis)].filter(v => v !== "—").join(" · ");
  const table = (heads, rows) => `<div class="effects-table-wrap" tabindex="0"><table><thead><tr>${heads.map(h => `<th scope="col">${escape(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${escape(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  const note = value => `<p class="effects-note">${escape(value)}</p>`;
  const state = { resource_type: "", action: "scale_in", status: "", q: "", from_local: "", to_local: "", page: 1, page_size: 20 };
  let host, initialized = false, listRequest = 0, detailRequest = 0;
  let charts = [];
  const localTime = value => {
    if (value == null || value === "") return null;
    const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
    if (!parts) throw new Error("请输入有效的本地日期和时间。");
    const values = parts.slice(1).map(v => Number(v || 0));
    const time = new Date(value);
    if (!Number.isFinite(time.getTime()) || [time.getFullYear(), time.getMonth() + 1, time.getDate(), time.getHours(), time.getMinutes(), time.getSeconds()].some((v, i) => v !== values[i])) throw new Error("请输入有效的本地日期和时间。");
    return time.getTime();
  };
  const query = (filters = state, paginated = true) => {
    const params = new URLSearchParams();
    for (const key of ["resource_type", "action", "status", "q", ...(paginated ? ["page", "page_size"] : [])]) {
      if (filters[key] !== "" && filters[key] != null) params.set(key, filters[key]);
    }
    const from = localTime(filters.from_local), to = localTime(filters.to_local);
    if (from != null && to != null && from >= to) throw new Error("调配结束时间必须晚于开始时间。");
    if (from != null) params.set("from_ms", from);
    if (to != null) params.set("to_ms", to);
    return params.toString();
  };
  const url = (suffix = "", paginated = true) => `/api/scaling-effects${suffix}?${query(state, paginated)}`;
  const detailUrl = (id, suffix = "") => `/api/scaling-effects/${encodeURIComponent(id)}${suffix}`;
  const seriesData = (series, field, maxGap) => {
    const points = [];
    (series.timestamps || []).forEach((time, index) => {
      if (index && maxGap > 0 && time - series.timestamps[index - 1] > maxGap) points.push([time - 1, null]);
      points.push([time, series[field]?.[index] ?? null]);
    });
    return points;
  };
  const disposeCharts = () => { charts.forEach(c => c.dispose()); charts = []; };
  const select = (name, title, options) => `<label>${title}<select name="${name}">${options.map(([value, text]) => `<option value="${value}"${state[name] === value ? " selected" : ""}>${text}</option>`).join("")}</select></label>`;

  function init() {
    if (initialized) return;
    host = document.getElementById("effects-view");
    if (!host) return;
    initialized = true;
    host.innerHTML = `<section class="effects-hero"><div class="effects-kicker">EXECUTION / OBSERVATION</div><h2>调配成效</h2><p>每一次真实调配，都有可追溯的前后对照。</p>${note("仅统计本系统 execute 任务；实测观察性对比不保证因果关系。缩容与扩容分别查看，CPU 与内存不相加。K8S Request / Limit 利用率不是物理集群利用率。")}</section>
      <form id="effects-filters" class="effects-filters" aria-label="成效独立筛选">
      ${select("resource_type", "资源类型", [["", "全部类型"], ["openstack_vm", "VM"], ["k8s_workload", "K8S Workload"]])}
      ${select("action", "调整方向", [["scale_in", "缩容"], ["scale_out", "扩容"], ["mixed", "混合调整"], ["unknown", "方向待核验"]])}
      ${select("status", "评估状态", [["", "全部状态"], ...Object.keys(labels).filter(k => ["executing", "awaiting_effective", "observing", "insufficient_data", "evaluated", "failed", "interrupted", "basis_changed", "missing_baseline", "capture_failed", "evidence_conflict", "expired"].includes(k)).map(k => [k, labels[k]])])}
      <label>资源 / 任务<input name="q" type="search" placeholder="搜索资源或任务 ID" /></label>
      <label>调配开始时间<input name="from_local" type="datetime-local" /></label><label>调配结束时间<input name="to_local" type="datetime-local" /></label>
      <button type="submit" class="primary-btn">查询 / 刷新</button>
      ${note("按事件发起时间筛选（本地时区），包含开始、不包含结束；不是观测窗口。留空表示不限。列表、CSV 和打印采用已查询的范围。")}</form>
      <div id="effects-filter-error" role="alert" hidden></div>
      <div class="effects-actions"><a id="effects-csv" class="secondary-btn" href="${escape(url("/export.csv", false))}">导出筛选范围 CSV</a><button id="effects-print" type="button" class="secondary-btn">打印报告</button></div>
      <div id="effects-result" aria-live="polite"></div><section id="effects-detail" aria-live="polite" hidden></section>`;
    host.querySelector("form").addEventListener("submit", event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const next = { ...state, page: 1 };
      for (const key of ["resource_type", "action", "status", "q", "from_local", "to_local"]) next[key] = String(form.get(key) || "").trim();
      const error = host.querySelector("#effects-filter-error");
      try { query(next); } catch (cause) {
        error.hidden = false;
        error.innerHTML = `<p class="effects-error">${escape(cause.message)} 当前报告和导出仍采用上次有效查询范围。</p>`;
        return;
      }
      error.hidden = true;
      Object.assign(state, next);
      load();
    });
    host.addEventListener("click", event => {
      const button = event.target.closest("button");
      if (!button) return;
      if (button.dataset.effectId) showDetail(button.dataset.effectId);
      if (button.dataset.effectsPage) { state.page = Number(button.dataset.effectsPage); load(); }
    });
    host.querySelector("#effects-print").addEventListener("click", () => window.print());
    window.addEventListener("resize", () => charts.forEach(c => c.resize()));
  }

  function renderSummary(summary) {
    const metrics = summary.metrics || [];
    return `<div class="effects-counts"><div><span>调配事件</span><strong>${number(summary.event_count)}</strong></div><div><span>去重资源</span><strong>${number(summary.resource_count)}</strong></div><div><span>当前方向</span><strong>${escape(label(state.action))}</strong></div></div>
      ${note(Object.entries(summary.status_counts || {}).map(([key, count]) => `${label(key)} ${number(count)}`).join(" · "))}
      ${note("平均值按调配事件统计，同一资源可多次计入。累计容量差不等于当前集群空闲容量或金额节省；只看缩容时不包含扩容消耗。")}
      ${metrics.length ? table(["指标 / 分母", "事件 / 相对变化有效数", "前 → 后利用率", "变化（百分点）", "平均相对变化", "加权相对变化", "释放容量", "释放容量 × 小时"], metrics.map(m => [metricName(m), `${number(m.event_count)} / ${number(m.relative_count)}`, `${pct(m.before_pct)} → ${pct(m.after_pct)}`, number(m.delta_pp, " pp"), pct(m.mean_relative_change_pct), pct(m.weighted_relative_change_pct), number(m.reclaimed_capacity, ` ${m.metric === "cpu" ? "cores" : "GiB"}`), number(m.reclaimed_unit_hours, ` ${m.metric === "cpu" ? "core·h" : "GiB·h"}`)])) : note("当前范围尚无可汇总指标。等待生效、缺失数据和失败事件仍保留在下方账本。")}
      ${note("变化百分点 = 后利用率 − 前利用率；相对变化 = 变化 / 前利用率，前值为 0 时不计算。正负结果均展示；扩容的负释放量表示新增容量。汇总按事件统计，不代表整个集群的收益。")}`;
  }

  async function load() {
    init();
    if (!host) return;
    const request = ++listRequest;
    ++detailRequest;
    disposeCharts();
    host.querySelector("#effects-detail").hidden = true;
    const result = host.querySelector("#effects-result");
    host.querySelector("#effects-csv").href = url("/export.csv", false);
    result.innerHTML = note("正在读取真实调配账本…");
    try {
      const payload = await window.ResourceApi.requestJson(url(), 1);
      if (request !== listRequest) return;
      const items = payload.items || [];
      result.innerHTML = renderSummary(payload.summary || {}) + note(`政策版本 ${payload.version ?? "—"} · 当前筛选独立于风险队列 · 调配发起时间：${state.from_local ? date(localTime(state.from_local)) + "（含）" : "不限开始"} → ${state.to_local ? date(localTime(state.to_local)) + "（不含）" : "不限结束"}（本地时区）`) +
        (items.length ? `<div class="effects-table-wrap" tabindex="0"><table><thead><tr><th>资源 / 任务</th><th>方向</th><th>状态</th><th>开始时间</th><th>证据</th></tr></thead><tbody>${items.map(e => `<tr><td><strong>${escape(e.resource_id)}</strong><small>${escape(e.task_id)}</small></td><td>${escape(label(e.action))}</td><td>${escape(label(e.status))}${e.reason ? `<small>${escape(e.reason)}</small>` : ""}</td><td>${escape(date(e.started_at_ms))}</td><td><button type="button" class="secondary-btn" data-effect-id="${escape(e.task_id)}">查看对照</button></td></tr>`).join("")}</tbody></table></div>` : `<div class="effects-empty"><h3>暂无符合条件的真实调配记录</h3><p>真实执行入账后将在这里展示。dry_run 不计入成效；没有监控证据时不会生成提升数据。</p></div>`) +
        `<div class="effects-pager"><button class="secondary-btn" type="button" data-effects-page="${Math.max(1, state.page - 1)}"${state.page <= 1 ? " disabled" : ""}>上一页</button><span>第 ${number(payload.page)} 页 · ${number(payload.total)} 个事件</span><button class="secondary-btn" type="button" data-effects-page="${state.page + 1}"${state.page * state.page_size >= (payload.total || 0) ? " disabled" : ""}>下一页</button></div>`;
    } catch (error) {
      if (request === listRequest) result.innerHTML = `<p class="effects-error" role="alert">账本加载失败：${escape(error.message || error)}。请点击查询 / 刷新重试。</p>`;
    }
  }

  function renderMetric(m, index) {
    const before = m.before || {}, after = m.after || {};
    return `<section class="effects-metric"><h4>${escape(metricName(m))} <span>${escape(label(m.status))}</span></h4>
      ${table(["观测窗口", "前窗口", "后窗口"], [
        ["时间范围", `${date(before.start_ms)} → ${date(before.end_ms)}`, `${date(after.start_ms)} → ${date(after.end_ms)}`],
        ["利用率（积分 usage / capacity）", pct(before.utilization_pct), pct(after.utilization_pct)],
        [`平均使用量（${m.unit || "—"}）`, number(before.mean_usage), number(after.mean_usage)],
        [`平均分母容量（${m.unit || "—"}）`, number(before.mean_capacity), number(after.mean_capacity)],
        ["覆盖率 / 有效小时", `${coverage(before.coverage)} / ${number(before.valid_hours, " h")}`, `${coverage(after.coverage)} / ${number(after.valid_hours, " h")}`],
        ["P95 利用率 / 超100%时长", `${pct(before.p95_pct)} / ${number(before.overload_hours, " h")}`, `${pct(after.p95_pct)} / ${number(after.overload_hours, " h")}`]
      ])}
      <div class="effects-deltas"><span>变化 <strong>${number(m.delta_pp, " pp")}</strong></span><span>相对变化 <strong>${pct(m.relative_change_pct)}</strong></span><span>释放容量 <strong>${number(m.reclaimed_capacity, ` ${escape(m.unit || "")}`)}</strong></span><span>容量缩减 <strong>${pct(m.capacity_reduction_pct)}</strong></span><span>释放容量时 <strong>${number(m.reclaimed_unit_hours, ` ${escape(m.unit || "")}·h`)}</strong></span></div>
      <div class="effects-chart" id="effects-chart-${index}" role="img" aria-label="${escape(metricName(m))} 前后真实使用量和容量曲线"></div></section>`;
  }

  async function showDetail(id) {
    const request = ++detailRequest;
    const target = host.querySelector("#effects-detail");
    disposeCharts();
    target.hidden = false;
    target.innerHTML = note("正在读取留档证据…");
    try {
      const payload = await window.ResourceApi.requestJson(detailUrl(id), 1);
      if (request !== detailRequest) return;
      const e = payload.event;
      if (!e) throw new Error("返回内容缺少调配事件");
      const metrics = e.metrics || payload.metrics || [];
      target.innerHTML = `<header class="effects-detail-head"><div><div class="effects-kicker">EVENT EVIDENCE</div><h3>${escape(e.resource_id)}</h3><small>${escape(e.task_id)} · ${escape(label(e.status))}</small></div><a class="secondary-btn" href="${escape(detailUrl(id, "/evidence.json"))}">导出证据 JSON</a></header>
        ${note(e.reason || "证据来自真实留档监控；历史容量缺失时不使用当前规格重建。")}
        ${note(`发起 ${date(e.started_at_ms)} · 命令完成 ${date(e.completed_at_ms)} · 实测生效 ${date(e.effective_at_ms)}`)}
        <details class="effects-evidence"><summary>冻结规格、统计口径与数据来源</summary><pre>${escape(JSON.stringify({ before_spec: e.before_spec, target_spec: e.target_spec, after_spec: e.after_spec, policy: e.policy, effective_confirmation: e.effective_confirmation, source: e.evidence_sources || e.source || "尚未收到真实监控证据", sha256: payload.sha256 }, null, 2))}</pre></details>
        ${metrics.length ? metrics.map(renderMetric).join("") : note("尚无可比指标。等待有效容量观测和完整窗口；缺失值显示为 —，不计为 0。")}`;
      const series = e.series || payload.series || [];
      metrics.forEach((m, index) => {
        const element = document.getElementById(`effects-chart-${index}`);
        const matches = series.filter(s => s.metric === m.metric && s.basis === m.basis && (!m.container || s.container === m.container));
        if (!window.echarts || !matches.length) { element.textContent = matches.length ? "图表组件不可用，可导出原始证据查看。" : "此指标暂无留档曲线，可查看窗口统计及证据包。"; return; }
        const chart = window.echarts.init(element, null, { renderer: "svg" });
        charts.push(chart);
        const windows = ["before", "after"].filter(phase => m[phase]?.start_ms != null && m[phase]?.end_ms != null).map(phase => [{ name: phase === "before" ? "前窗口" : "后窗口", xAxis: m[phase].start_ms, itemStyle: { color: phase === "before" ? "rgba(100,116,139,.06)" : "rgba(15,118,110,.06)" } }, { xAxis: m[phase].end_ms }]);
        chart.setOption({ animation: false, color: ["#0f766e", "#d97706", "#2563eb", "#64748b"], tooltip: { trigger: "axis", renderMode: "richText" }, legend: { type: "scroll", top: 0 }, grid: { left: 60, right: 25, top: 42, bottom: 65 }, xAxis: { type: "time" }, yAxis: { type: "value", name: m.unit || "" }, dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 12 }], series: matches.flatMap(s => ["usage", "capacity"].map(field => ({ name: `${s.container || "VM"} ${field === "usage" ? "使用量" : label(s.basis)}`, type: "line", showSymbol: false, connectNulls: false, lineStyle: { type: field === "capacity" ? "dashed" : "solid" }, data: seriesData(s, field, e.policy?.max_gap_ms), markArea: { silent: true, data: windows }, markLine: e.effective_at_ms ? { symbol: "none", data: [{ xAxis: e.effective_at_ms, label: { formatter: "实测生效" } }] } : undefined }))) });
      });
    } catch (error) {
      if (request === detailRequest) target.innerHTML = `<p class="effects-error" role="alert">证据加载失败：${escape(error.message || error)}</p>`;
    }
  }
  window.ScalingEffects = { init, load, showDetail, query, number, renderMetric, renderSummary, detailUrl, seriesData };
})();
