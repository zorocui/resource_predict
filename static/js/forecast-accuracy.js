(function () {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const number = (value, suffix = "") => finite(value) ? `${value.toLocaleString("zh-CN", { maximumFractionDigits: 3 })}${suffix}` : "—";
  const rate = value => finite(value) ? number(value * 100, "%") : "—";
  const date = value => finite(value) ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
  const labels = { realized: "预测兑现评估", holdout: "独立历史测试", legacy: "旧报告参考", resource: "资源", container: "容器", openstack_vm: "VM", k8s_workload: "K8S Workload", cpu: "CPU", memory: "内存", matched: "有效配对", awaiting_target: "尚未到期", awaiting_observation: "缺少实测", missing_provenance: "来源证据不足", not_future_at_publication: "非事前预测", nonfinite_observation: "无效或缺失观测", invalid_prediction: "预测失败或数值无效", basis_mismatch: "口径不匹配", unsupported_unit: "单位不支持", legacy_unverified: "旧报告 · 未核验" };
  const label = value => labels[value] || value || "—";
  const unit = value => value === "percentage_points" ? "pp（百分点）" : value || "单位未知";
  const note = text => `<p class="accuracy-note">${escape(text)}</p>`;
  const table = (heads, rows) => `<div class="accuracy-table-wrap" tabindex="0"><table><thead><tr>${heads.map(v => `<th scope="col">${escape(v)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(v => `<td>${escape(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  const keys = ["source", "resource_type", "level", "metric", "model", "horizon", "q"];
  const state = { source: "realized", resource_type: "", level: "resource", metric: "", model: "", horizon: "", q: "", period: "all", from_local: "", to_local: "", page: 1, page_size: 50 };
  let host, chart, items = [], sequence = 0, ready = false, saving = false, searchTimer;
  let modelOptions = new Set();
  const metricLabels = { cpu: "CPU", memory: "内存", disk: "磁盘", cpu_request: "CPU · Request", cpu_limit: "CPU · Limit", memory_request: "内存 · Request", memory_limit: "内存 · Limit" };
  const modelLabels = { arima: "ARIMA", sarima: "SARIMA", prophet: "Prophet", seasonal_naive: "季节基线", rolling_mean: "滚动均值", ensemble: "集成模型", lstm: "LSTM" };
  const localTime = value => {
    if (!value) return null;
    const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
    const d = new Date(value);
    if (!parts || !Number.isFinite(d.getTime()) || [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds()].some((v, i) => v !== Number(parts[i + 1] || 0))) throw new Error("请输入有效的本地日期和时间。");
    return d.getTime();
  };
  function query(filters = state, paginated = true) {
    const params = new URLSearchParams();
    for (const key of [...keys, ...(paginated ? ["page", "page_size"] : [])]) if (filters[key] != null && filters[key] !== "") params.set(key, filters[key]);
    const from = filters.from_ms ?? localTime(filters.from_local), to = filters.to_ms ?? localTime(filters.to_local);
    if (from != null && to != null && from >= to) throw new Error("目标结束时间必须晚于开始时间。");
    if (from != null) params.set("from_ms", from);
    if (to != null) params.set("to_ms", to);
    return params.toString();
  }
  function periodRange(period, now = Date.now()) {
    if (period === "24h" || period === "7d") return { from_ms: now - (period === "24h" ? 24 : 168) * 3600000, to_ms: now };
    return { from_ms: null, to_ms: null };
  }
  function refreshOptions(payload = {}) {
    const form = host.querySelector("form");
    const metrics = state.resource_type === "openstack_vm" ? ["cpu", "memory", "disk"] : state.resource_type === "k8s_workload" ? ["cpu_request", "cpu_limit", "memory_request", "memory_limit"] : Object.keys(metricLabels);
    for (const row of [...(payload.summary || []), ...(payload.items || [])]) if (row.model) modelOptions.add(String(row.model));
    if (state.model) modelOptions.add(state.model);
    for (const [name, values, titles, all] of [["metric", metrics, metricLabels, "全部指标"], ["model", [...modelOptions].sort(), modelLabels, "全部模型"]]) {
      const node = form.querySelector(`[name="${name}"]`);
      if (!node) continue;
      const options = new Set(values);
      if (state[name]) options.add(state[name]);
      node.innerHTML = `<option value="">${all}</option>` + [...options].map(value => `<option value="${escape(value)}">${escape(titles[value] || value)}</option>`).join("");
      node.value = state[name];
    }
  }
  function applyFilters(event) {
    event?.preventDefault();
    clearTimeout(searchTimer);
    const form = host.querySelector("form"), values = new FormData(form);
    const next = { ...state, page: 1 };
    for (const key of [...keys, "period", "from_local", "to_local"]) next[key] = String(values.get(key) ?? state[key] ?? "").trim();
    if (next.source !== state.source || next.resource_type !== state.resource_type || next.level !== state.level) {
      next.metric = next.model = "";
      modelOptions = new Set();
    }
    if (next.source === "legacy") next.period = "all";
    if (next.period !== "custom") next.from_local = next.to_local = "";
    Object.assign(next, periodRange(next.period));
    const error = host.querySelector("#accuracy-filter-error");
    try { query(next); } catch (cause) { error.textContent = `${cause.message} 当前报告和导出仍使用上次有效筛选。`; return; }
    error.textContent = "";
    Object.assign(state, next);
    form.querySelector('[name="period"]').value = state.period;
    form.querySelector('[name="period"]').disabled = state.source === "legacy";
    const custom = form.querySelector(".accuracy-custom-range");
    custom.hidden = state.period !== "custom";
    if (!custom.hidden) form.querySelector("details").open = true;
    refreshOptions();
    load();
  }
  const identity = row => [label(row.resource_type || row.type), row.resource_id, row.container || label(row.level || "resource"), label(row.metric), row.model, row.basis_unit || row.unit, row.horizon].filter(Boolean).join(" · ");
  function renderSummary(payload) {
    const c = payload.coverage || {};
    const legacy = payload.source === "legacy";
    return note(legacy ? "旧报告缺少可核验逐点证据，不能追认为独立测试或预测兑现；原始误差单位以旧报告为准，容差达标率不可计算。" : "±5 / ±10 个百分点为含边界的业务容差规则，不是通用准确率标准、置信度或 100% − MAPE。绝对量只报告 cores / GiB 误差。") +
      `<div class="accuracy-counts">${[["候选点（模型筛选前）", c.candidate_points], ["选中点", c.selected_points], ["去重排除（模型筛选前）", c.duplicate_points], ["到期点", c.due_points], ["有效配对", c.matched_points]].map(([name, value]) => `<div><span>${name}</span><strong>${number(value)}</strong></div>`).join("")}<div><span>到期观测完整率</span><strong>${rate(c.observation_coverage)}</strong></div></div>` +
      note(Object.entries(c.status_counts || {}).map(([key, value]) => `${label(key)} ${number(value)}`).join(" · ") || "无完整性统计证据。") +
      (legacy ? note(`旧报告包含 ${number(c.legacy_report_rows)} 条汇总误差记录、${number(c.resource_count)} 个资源；这些不是已核验的预测点数。`) : "") +
      ((payload.summary || []).length ? table(["类型 / 层级 / 指标 / 模型 / 口径 / 提前量", "有效点 / 资源 / 等权组", "MAE", "RMSE", "P95 绝对误差", "±5pp 点达标 / 等权达标", "±10pp 点达标 / 等权达标", "低估比例"], payload.summary.map(row => [identity(row), `${number(row.count)} / ${number(row.resource_count)} / ${number(row.cohort_count)}`, number(row.mae, ` ${unit(row.unit)}`), number(row.rmse, ` ${unit(row.unit)}`), number(row.p95_error, ` ${unit(row.unit)}`), `${rate(row.hit_rate_5pp)} / ${rate(row.macro_hit_rate_5pp)}`, `${rate(row.hit_rate_10pp)} / ${rate(row.macro_hit_rate_10pp)}`, rate(row.underestimate_rate)])) : note("当前范围没有可汇总的有效配对点；缺失证据不表示误差为零或预测准确。")) +
      note("点达标率以有效配对点为分母；等权达标率先按资源/容器组计算再等权平均。资源层与容器层分别查询，指标及 Request / Limit 口径不合并。误差 = 预测 − 实际；P95 使用 nearest rank。明细中非「有效配对」行的实际值即使存在，也未经同口径核验，不参与统计与对照图。");
  }
  function renderItems(payload) {
    const rows = payload.items || [];
    if (!rows.length) return note("当前筛选没有逐点记录。可切换评估来源或调整筛选范围。");
    if (payload.source === "legacy") return table(["资源 / 容器 / 指标 / 模型", "原始 RMSE", "原始 MAE", "原始 MAPE", "原始 P95", "证据状态"], rows.map(row => [identity(row), number(row.rmse), number(row.mae), number(row.mape), number(row.p95_error), "旧报告未核验 · 无逐点预测/实际证据"]));
    return `<div class="accuracy-table-wrap" tabindex="0"><table><thead><tr>${["资源 / 容器 / 指标 / 模型 / 口径", "目标时间 / 提前量", "冻结预测", "实际", "误差", "状态 / ±5pp / ±10pp", "对照"].map(h => `<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>${rows.map((row, i) => `<tr><td>${escape(identity(row))}</td><td>${escape(date(row.target_ms))}<br>${escape(row.horizon)}</td><td>${number(row.predicted)} ${escape(row.unit === "percentage_points" ? "%" : unit(row.unit))}</td><td>${number(row.actual)} ${escape(row.unit === "percentage_points" ? "%" : unit(row.unit))}</td><td>${number(row.error)} ${escape(unit(row.unit))}</td><td>${escape(label(row.status))} / ${row.hit_5pp == null ? "—" : row.hit_5pp ? "达标" : "未达标"} / ${row.hit_10pp == null ? "—" : row.hit_10pp ? "达标" : "未达标"}</td><td><button type="button" class="secondary-btn" data-accuracy-point="${i}">查看对照</button></td></tr>`).join("")}</tbody></table></div>`;
  }
  function curvePoints(rows, selected) {
    const fields = ["resource_type", "resource_id", "container", "metric", "model", "batch", "basis_unit", "unit"];
    return rows.filter(row => fields.every(k => row[k] === selected[k])).sort((a, b) => a.target_ms - b.target_ms);
  }
  function showCurve(index, scroll = true) {
    const selected = items[index];
    if (!selected) return;
    const target = host.querySelector("#accuracy-curve");
    chart?.dispose(); chart = null;
    target.hidden = false;
    target.innerHTML = `<h3>冻结预测与实际对照</h3>${note(`${identity(selected)} · 批次 ${selected.batch || "—"}`)}${note("仅显示当前页同资源、容器、模型、批次及口径的离散点，不连接时间空档。全量证据请导出或保存快照；实测无有效配对的点不进入实际对照。阴影为冻结预测 ±5pp 容差。")}${note(`留档时间代理：${date(selected.issued_ms)} · 训练截止：${date(selected.data_end_ms)}`)}<div class="accuracy-chart" role="img" aria-label="当前页冻结预测与实际离散点图"></div>`;
    if (!window.echarts) { target.innerHTML += note("图表库未加载，请查看逐点表格。"); return; }
    const rows = curvePoints(items, selected);
    const series = ["predicted", "actual"].map(field => ({ name: field === "predicted" ? "冻结预测" : "有效实际", type: "scatter", symbolSize: 8, data: rows.map(row => [row.target_ms, field === "actual" && row.status !== "matched" ? null : row[field]]) }));
    if (selected.unit === "percentage_points") series[0].markArea = { silent: true, itemStyle: { color: "rgba(37,99,235,.12)" }, data: rows.filter(row => finite(row.predicted)).map(row => [{ xAxis: row.target_ms - 30000, yAxis: row.predicted - 5 }, { xAxis: row.target_ms + 30000, yAxis: row.predicted + 5 }]) };
    chart = window.echarts.init(target.querySelector(".accuracy-chart"));
    chart.setOption({ color: ["#2563eb", "#c05a1b"], tooltip: { trigger: "item", renderMode: "richText" }, legend: {}, grid: { left: 65, right: 25, top: 45, bottom: 65 }, xAxis: { type: "time" }, yAxis: { type: "value", name: selected.unit === "percentage_points" ? "利用率 %" : unit(selected.unit) }, dataZoom: [{ type: "inside" }, { type: "slider", height: 18 }], series });
    if (scroll) target.scrollIntoView?.({ block: "nearest" });
  }
  function renderSnapshot(payload, filters) {
    const href = String(payload.download_url || "");
    if (!/^\/api\/forecast-accuracy\/snapshots\/[A-Za-z0-9_-]+(?:\/download)?$/.test(href)) throw new Error("快照下载地址无效。");
    return `${note(`快照已完成：${payload.snapshot_id} · ${number(payload.point_count)} 个证据点${payload.record_count == null ? "" : ` · ${number(payload.record_count)} 条记录`} · SHA256 ${payload.sha256 || "—"}`)}${note(`冻结来源和筛选：${filters}`)}${new URLSearchParams(filters).get("source") === "legacy" ? note("旧报告快照仅保存参考记录，不包含可核验预测/实际配对证据。") : ""}<a class="secondary-btn" href="${escape(href)}">下载已冻结报告包</a>`;
  }
  async function saveSnapshot() {
    const button = host.querySelector("#accuracy-snapshot");
    const output = host.querySelector("#accuracy-snapshot-result");
    const filters = query(state, false);
    if (saving || !ready) return;
    saving = true;
    button.disabled = true; output.innerHTML = note("正在冻结本次筛选的汇总及全部证据；完成后提供下载。");
    try {
      const response = await fetch("/api/forecast-accuracy/snapshots", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(new URLSearchParams(filters))) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      output.innerHTML = renderSnapshot(payload, filters);
      const download = document.createElement("a");
      download.href = payload.download_url;
      download.download = "";
      document.body.appendChild(download);
      download.click();
      download.remove();
    } catch (error) { output.innerHTML = `<p class="accuracy-error" role="alert">快照未完成：${escape(error.message)}</p>`; }
    finally { saving = false; button.disabled = !ready; }
  }
  const warningText = text => ({ "Independent tests use preprocessed historical data, not production observations.": "独立测试来自预处理历史数据，不等于生产实测。", "issued_ms is an archive/generation-time proxy, not confirmed frontend publication time.": "issued_ms 是留档/生成时间代理，不等于前端正式可见时间。", "Realized evidence evaluates selected models; it is not a fair all-model comparison.": "兑现证据只评估已选择的模型，不适合据此公平比较所有模型。", "No archived point evidence is available; historical reports are not backfilled.": "尚无逐点留档证据；旧报告不会回填为新证据。" }[text] || text);
  async function load() {
    init(); if (!host) return;
    const request = ++sequence;
    ready = false; items = []; chart?.dispose(); chart = null;
    host.querySelector("#accuracy-curve").hidden = true;
    host.querySelector("#accuracy-snapshot").disabled = true;
    const result = host.querySelector("#accuracy-result");
    for (const kind of ["summary", "points"]) host.querySelector(`#accuracy-${kind}-csv`).href = `/api/forecast-accuracy/export.csv?kind=${kind}&${query(state, false)}`;
    host.querySelector("#accuracy-points-csv").hidden = state.source === "legacy";
    result.innerHTML = note("正在读取留档评估证据…");
    try {
      const payload = await window.ResourceApi.requestJson(`/api/forecast-accuracy?${query()}`, 1);
      if (request !== sequence) return;
      items = payload.items || []; ready = true;
      host.querySelector("#accuracy-snapshot").disabled = saving;
      refreshOptions(payload);
      result.innerHTML = `<h3>${escape(label(payload.source))}</h3>${note(`查询生成：${date(payload.generated_at_ms)} · 筛选：${query(state, false)}`)}${(payload.warnings || []).map(w => note(warningText(w))).join("")}${renderSummary(payload)}<h3>证据明细</h3>${renderItems(payload)}<div class="accuracy-pages"><button type="button" class="secondary-btn" data-accuracy-page="${state.page - 1}" ${state.page <= 1 ? "disabled" : ""}>上一页</button><span>第 ${number(payload.page)} 页 · 共 ${number(payload.total)} 条</span><button type="button" class="secondary-btn" data-accuracy-page="${state.page + 1}" ${state.page * state.page_size >= (payload.total || 0) ? "disabled" : ""}>下一页</button></div>`;
      const first = items.findIndex(row => row.status === "matched");
      if (first >= 0 && payload.source !== "legacy") showCurve(first, false);
    } catch (error) { if (request === sequence) result.innerHTML = `<p class="accuracy-error" role="alert">评估加载失败：${escape(error.message || error)}。尚未取得可核验结果，请重试。</p>`; }
  }
  function init() {
    if (host) return;
    host = document.getElementById("accuracy-view"); if (!host) return;
    const select = (name, title, options) => `<label>${title}<select name="${name}">${options.map(([value, text]) => `<option value="${escape(value)}"${state[name] === value ? " selected" : ""}>${escape(text)}</option>`).join("")}</select></label>`;
    host.innerHTML = `<section class="accuracy-hero"><div class="accuracy-kicker">FORECAST / EVIDENCE</div><h2>预测准确性</h2><p>打开即可查看全部可用评估，选择条件后自动刷新。</p>${note("兑现评估、独立测试与旧报告分别统计。长期佐证可一键保存并下载完整报告。")}</section>
      <form class="accuracy-filters" aria-label="预测准确性独立筛选">
        <fieldset class="accuracy-source-tabs"><legend>评估来源</legend>${["realized", "holdout", "legacy"].map(value => `<label><input type="radio" name="source" value="${value}"${state.source === value ? " checked" : ""} /><span>${labels[value]}</span></label>`).join("")}</fieldset>
        ${select("resource_type", "资源类型", [["", "全部资源"], ["openstack_vm", "VM"], ["k8s_workload", "K8S Workload"]])}
        ${select("metric", "指标", [["", "全部指标"]])}
        ${select("period", "目标时间范围", [["all", "全部可用数据"], ["24h", "最近24小时"], ["7d", "最近7天"], ["custom", "自定义时间"]])}
        <button type="submit" class="secondary-btn">刷新</button>
        <details class="accuracy-advanced"><summary>高级筛选（可选）</summary><div class="accuracy-advanced-fields">
          ${select("level", "统计层级", [["resource", "资源"], ["container", "容器"]])}
          ${select("model", "模型", [["", "全部模型"]])}
          ${select("horizon", "预测提前量", [["", "全部提前量"], ...["0-1h", "1-6h", "6-24h", ">24h"].map(v => [v, v])])}
          <label>查找资源（可选）<input name="q" type="search" placeholder="输入后自动查找" /></label>
          <div class="accuracy-custom-range" hidden><label>目标开始时间（含）<input name="from_local" type="datetime-local" /></label><label>目标结束时间（不含）<input name="to_local" type="datetime-local" /></label></div>
          <button id="accuracy-reset" type="button" class="secondary-btn">重置筛选</button>
        </div></details>
      </form><div id="accuracy-filter-error" role="alert"></div>
      <div class="accuracy-actions"><button id="accuracy-snapshot" type="button" class="primary-btn" disabled>保存并下载报告</button><button id="accuracy-print" type="button" class="secondary-btn">打印当前报告</button><details class="accuracy-export-more"><summary>更多导出</summary><a id="accuracy-summary-csv" class="secondary-btn">汇总 CSV</a><a id="accuracy-points-csv" class="secondary-btn">全部证据 CSV</a></details></div>
      <div id="accuracy-snapshot-result" aria-live="polite"></div><div id="accuracy-result" aria-live="polite"></div><section id="accuracy-curve" hidden></section>`;
    const form = host.querySelector("form");
    form.addEventListener("submit", applyFilters);
    form.addEventListener("change", event => {
      if (event.target.name === "q") return;
      applyFilters();
    });
    form.addEventListener("input", event => {
      if (event.target.name !== "q") return;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(applyFilters, 400);
    });
    host.querySelector("#accuracy-reset").addEventListener("click", () => { form.reset(); applyFilters(); });
    refreshOptions();
    host.addEventListener("click", event => {
      const button = event.target.closest("button"); if (!button || button.disabled) return;
      if (button.dataset.accuracyPage) { state.page = Number(button.dataset.accuracyPage); load(); }
      if (button.dataset.accuracyPoint != null) showCurve(Number(button.dataset.accuracyPoint));
    });
    host.querySelector("#accuracy-snapshot").addEventListener("click", saveSnapshot);
    host.querySelector("#accuracy-print").addEventListener("click", () => window.print());
    window.addEventListener("resize", () => chart?.resize());
  }
  window.ForecastAccuracy = { init, load, query, number, rate, renderSummary, renderItems, curvePoints, renderSnapshot, saveSnapshot, periodRange };
})();
