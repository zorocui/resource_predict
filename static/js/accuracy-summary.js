(function () {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = value => Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : "—";
  const rate = value => Number.isFinite(value) ? `${number(value * 100)}%` : "—";
  const date = value => Number.isFinite(value) ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
  let host, sequence = 0;
  function render(data) {
    const times = data.rows.flatMap(row => [row.test_start_ms, row.test_end_ms]).filter(Number.isFinite);
    const range = times.reduce((v, t) => [Math.min(v[0], t), Math.max(v[1], t)], [Infinity, -Infinity]);
    return `<h2>预测准确率</h2><p>历史留出测试 · 只评估实际选用的模型 · 允许误差取 5 个百分点与实际值绝对值的 5% 中较大者</p>
      <div class="accuracy-actions"><strong style="font-size:2.5rem">${rate(data.accuracy)}</strong><span>测试资源 ${number(data.resource_count)} 个</span><span>有效测试点 ${number(data.valid_points)} 个</span><span>达标点 ${number(data.hit_points)} 个</span></div>
      <p>计算方式：达标点 ÷ 有效测试点。K8S 优先采用容器测试，避免与 Workload 汇总重复统计。</p>
      <p>例如实际使用率 4000%，允许相差 200 个百分点（预测 3800%～4200% 达标）；实际使用率 50%，允许相差 5 个百分点。平均绝对误差仍按百分点报告。</p>
      ${(data.needs_regeneration || []).length ? `<p role="status">${escape(data.needs_regeneration.join("、"))} 的汇总仍为旧口径，未计入当前准确率；请重新预测以生成新口径结果。</p>` : ""}
      <p>测试时间：${date(range[0])} 至 ${date(range[1])}</p>
      <p>${data.runs.map(run => `${escape(run.scope)} 更新于 ${date(run.generated_at_ms)}`).join("；") || "尚未生成简化版评估。下一次预测完成后自动显示，无需手动评估。"}</p>
      <p>${data.runs.some(run => run.mixed_prediction_runs) ? "包含单资源局部更新：各资源保留各自最近的测试结果，预测批次可能不同。" : "每类资源采用最近一次预测运行的测试汇总，增量预测仅代表本次重算范围。"}这是经过预处理的历史数据测试，不代表未来预测已兑现。</p>
      ${data.invalid_points ? `<p>无效或非独立测试点：${number(data.invalid_points)}，未计入准确率。</p>` : ""}
      ${data.absolute_unit_points ? `<p>另有 ${number(data.absolute_unit_points)} 个核数、GiB 或其他非百分比点，仅报告误差，不计入准确率。</p>` : ""}
      ${data.valid_points === 0 && data.runs.length ? "<p>本次没有可计算百分比准确率的有效测试点，准确率显示 —，不是 0%。</p>" : ""}
      <div class="accuracy-actions"><button id="accuracy-summary-refresh" class="secondary-btn">刷新</button><button id="accuracy-summary-export" class="primary-btn" ${data.runs.length ? "" : "disabled"}>导出汇总 CSV</button><button id="accuracy-summary-print" class="secondary-btn">打印 / 保存 PDF</button></div>
      <p>下表显示前 100 条测试汇总；导出包含全部记录。</p>
      <div class="accuracy-table-wrap"><table><thead><tr><th>资源 / 容器</th><th>指标</th><th>模型</th><th>准确率</th><th>有效点</th><th>平均绝对误差</th></tr></thead><tbody>${data.rows.slice(0, 100).map(row => `<tr><td>${escape(row.resource_id)}${row.container ? ` / ${escape(row.container)}` : ""}</td><td>${escape(({cpu:"CPU",memory:"内存",disk:"磁盘",cpu_limit:"CPU / Limit",cpu_request:"CPU / Request",memory_limit:"内存 / Limit",memory_request:"内存 / Request"})[row.metric] || row.metric)}</td><td>${escape(row.model)}</td><td>${rate(row.accuracy)}</td><td>${number(row.valid_points)}</td><td>${number(row.mae)} ${escape(row.unit)}</td></tr>`).join("")}</tbody></table></div>`;
  }
  function csv(data) {
    const fields = ["resource_id", "resource_type", "container", "metric", "model", "accuracy", "valid_points", "hit_points", "invalid_points", "mae", "unit", "test_start_ms", "test_end_ms"];
    const cell = v => `"${String(v ?? "").replace(/^[=+@-]/, "'$&").replace(/"/g, '""')}"`;
    const lines = [["评估口径", "独立历史测试；准确率为绝对误差≤max(5个百分点, 实际值绝对值×5%)的有效点比例"], ["总准确率", data.accuracy], ["有效点", data.valid_points], ["达标点", data.hit_points], ["待重新预测范围（旧口径未计入）", (data.needs_regeneration || []).join("；")], ...data.runs.map(r => [r.scope, r.generated_at_ms]), fields, ...data.rows.map(r => fields.map(f => r[f]))];
    return "\ufeff" + lines.map(row => row.map(cell).join(",")).join("\r\n");
  }
  async function load() {
    if (!host) return;
    const current = ++sequence;
    host.innerHTML = "<p>正在读取准确率汇总…</p>";
    try {
      const response = await fetch("/api/forecast-accuracy/summary", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "读取失败");
      if (current !== sequence) return;
      host.innerHTML = render(data);
      host.querySelector("#accuracy-summary-refresh").onclick = load;
      host.querySelector("#accuracy-summary-print").onclick = () => window.print();
      host.querySelector("#accuracy-summary-export").onclick = () => {
        const url = URL.createObjectURL(new Blob([csv(data)], { type: "text/csv;charset=utf-8" }));
        const link = document.createElement("a");
        link.href = url; link.download = "forecast-accuracy-summary.csv"; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      };
    } catch (error) {
      if (current !== sequence) return;
      host.innerHTML = `<p role="alert">${escape(error.message)}，不能作为无数据处理。</p><button id="accuracy-summary-refresh">重试</button>`;
      host.querySelector("#accuracy-summary-refresh").onclick = load;
    }
  }
  window.ForecastAccuracy = { init() { host = document.getElementById("accuracy-view"); }, load, render, csv };
})();
