(function () {
  const host = document.getElementById("feedback-content");
  const escape = value => window.ResourceList.escapeHtml(value);
  let requestId = 0;
  const labels = {
    calibrated: "校准完成", partial: "部分时段样本不足", insufficient_samples: "样本不足",
    missing_provenance: "缺少预测来源", failed: "计算失败", basis_changed: "当前规格已变化",
    incomplete_or_incomparable_calibration: "上界不完整或口径不可比", incomplete_fresh_prediction: "等待完整重算",
    incomplete_container_prediction: "容器预测不完整", missing_container_specs: "缺少容器规格",
    insufficient_continuous_paired_runs: "连续可比轮次不足", insufficient_transitions: "建议对照轮次不足",
    recommendation_changes_increased: "新建议变更更频繁", metric_checks_failed: "部分指标未通过检查",
    insufficient_reservation_benefit: "预留收益不足", latest_run_not_paired: "最新预测尚未形成完整对照",
    stale_or_future_prediction: "预测时间不满足新鲜度要求", stale_or_future_forecast_data: "预测使用的数据已陈旧",
    incomplete_or_incomparable_latest_budgets: "缺少完整可比的资源预算", invalid_evidence: "证据无效",
    sample_count: "真实样本数不足", time_span: "观测跨度不足", observation_coverage: "观测完整率不足",
    fresh_observations: "最近真实观测已陈旧", exceedance_rate: "超出比例增加", excess_magnitude: "超出幅度增加",
    allocation_not_increased: "资源分配量增加", current_spec_changed: "当前规格已变化",
    disabled: "受控启用开关关闭", resource_not_allowlisted: "该资源未列入启用范围",
    not_a_fresh_prediction: "等待该资源重新预测", assessment_expired: "启用判定已过期",
    fresh_report_unavailable: "等待最新报告", fresh_archive_unavailable: "本轮预测留档不可用",
    assessment_not_eligible_for_current_batch: "当前批次尚不具备启用条件",
    stale_assessment: "启用判定报告已陈旧", partial_rerun: "本轮只重算部分指标",
    missing_capacity: "缺少容量基准", replicas_changed: "副本数变化，不能直接回放容量效果",
    non_ratio_metric: "绝对值口径，不能直接回放容量效果",
  };
  const text = code => labels[code] || (code ? `待核验：${code}` : "暂无结果");
  const number = (v, digits = 2) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: digits });
  const percent = v => v == null ? "—" : `${number(Number(v) * 100, 1)}%`;
  const date = v => v && Number.isFinite(Number(v)) ? new Date(v).toLocaleString("zh-CN", { hour12: false }) : "—";
  const metric = (r, m, c) => `${c ? `${c} · ` : ""}${window.ResourceList.metricTitleFor(r, m) || m}`;
  const notice = s => `<p class="feedback-note">${escape(s)}</p>`;
  const table = (heads, rows) => `<div class="feedback-table-wrap" tabindex="0"><table class="feedback-table"><thead><tr>${heads.map(h => `<th scope="col">${escape(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${escape(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;

  function render(resource, payload) {
    const advice = resource.scaling_advice || {};
    const activation = advice.calibration_activation || {};
    const now = payload.server_time_ms || Date.now();
    const active = activation.status === "active" && activation.valid_until_epoch_ms > now && payload.policy_enabled && payload.resource_allowlisted;
    const needsReview = activation.status === "active" && !active;
    const assessment = payload.assessment;
    const expired = assessment?.status === "eligible_for_review" && !(assessment.valid_until_epoch_ms > now);
    const ready = payload.report_status === "available" && !expired && assessment?.status === "eligible_for_review";
    const upper = advice.prediction_upper_bound?.metrics || [];
    const shadow = resource.shadow_comparison || {};
    const paired = shadow.status === "paired";
    const reportState = { missing: "尚无真实反馈报告", stale: "报告已陈旧，请更新预测后查看", error: "报告读取失败，请稍后重试" };
    let html = `<div class="feedback-hero"><div class="feedback-eyebrow">预测反馈 · ${escape(window.ResourceList.typeLabel(resource))}</div><div class="feedback-heading"><h3>${active ? "正式采用校准建议" : needsReview ? "校准建议待重新核验" : "观察与验证中"}</h3><span class="feedback-pill ${active ? "is-active" : ""}">${active ? "受控采用" : needsReview ? "等待重新预测" : "基线策略"}</span></div>${notice(active ? "当前建议已采用校准上界；实际执行仍需通过全部门控。" : needsReview ? "旧校准建议不再具备有效采用条件，需要重新预测后确认回退结果。" : "校准和影子对照仅用于验证，正式建议继续使用原策略。")}</div>`;
    html += `<div class="feedback-stages">${[["01", "预测上界", upper.length ? `${upper.filter(m => m.complete).length} / ${upper.length} 指标完整` : "等待新版预测"], ["02", "影子对照", paired ? "已形成配对" : "等待可比样本"], ["03", "启用评审", ready ? "具备评审条件" : "继续观察"]].map(s => `<div><span>${s[0]}</span><strong>${s[1]}</strong><small>${escape(s[2])}</small></div>`).join("")}</div>`;
    html += `<section class="feedback-section"><h4>策略状态</h4><dl class="feedback-facts"><div><dt>受控启用开关</dt><dd>${payload.policy_enabled ? "已开启" : "关闭"}</dd></div><div><dt>当前资源</dt><dd>${payload.resource_allowlisted ? "已列入允许范围" : "未列入允许范围"}</dd></div><div><dt>报告更新时间</dt><dd>${escape(date(payload.report_generated_at_ms))}</dd></div></dl>`;
    if (activation.status === "active" && !active) html += notice("此前采用的校准建议已失效或配置已关闭，请重新预测；不可将旧结果作为执行依据。");
    if (activation.reason && activation.status !== "active") html += notice(text(activation.reason));
    html += `</section><section class="feedback-section"><h4>预测上界 <span>目标覆盖率 95%</span></h4>${notice("目标覆盖率不是已验证的准确率；缺样本的时段不会补成 0。可在指标图例中开关校准上界。")}`;
    html += upper.length ? table(["指标", "窗口上界峰值", "状态"], upper.map(m => [metric(resource, m.metric, m.container), m.upper_peak == null ? "—" : `${number(m.upper_peak)} ${m.unit?.includes("/") || m.unit?.endsWith(":ratio") ? "倍基准" : m.unit?.includes("cores") ? "核" : m.unit?.includes("gb") ? "GiB" : "原指标单位"}`, text(m.status)])) : notice("当前产物没有校准上界。运行新版完整预测后，样本充足的指标会在此展示；现有资源数据未被改写。");
    html += `</section><section class="feedback-section"><h4>影子建议对照 <span>不执行对照方案</span></h4>`;
    if (paired) {
      html += `<div class="feedback-comparison"><div><small>原策略</small><strong>${escape(window.ResourceList.actionLabel(shadow.baseline?.action))}</strong></div><span>对照</span><div><small>校准方案</small><strong>${escape(window.ResourceList.actionLabel(shadow.candidate?.action))}</strong></div></div>`;
      html += table(["指标", "原分配", "校准分配", "说明"], (shadow.budgets || []).map(b => [metric(resource, b.metric, b.container), `${number(b.baseline_allocation)} ${b.unit || ""}`, `${number(b.shadow_allocation)} ${b.unit || ""}`, b.skip_reason ? text(b.skip_reason) : b.role === "request_budget" ? "request 预留预算" : "容量预算"]));
      html += notice("K8S 资源量为每副本规格 × 目标副本数；预算超出不等同于业务故障。这里是预测时冻结的两套建议。");
    } else html += notice(shadow.reason ? text(shadow.reason) : "尚无完整影子对照。需要全部指标和容器拥有完整、同口径的校准上界。");
    html += `</section><section class="feedback-section"><h4>启用判定 <span class="feedback-pill ${ready ? "is-active" : ""}">${ready ? "具备启用评审条件" : expired ? "判定已过期" : "继续观察"}</span></h4>`;
    if (payload.report_status !== "available") html += notice(reportState[payload.report_status] || "反馈请求失败，请重试。");
    if (!assessment) html += notice("此资源还没有评审证据。不会将缺少数据视为已通过，也不会自动启用新策略。");
    else {
      html += `<dl class="feedback-facts"><div><dt>连续可比轮次</dt><dd>${number(assessment.paired_runs, 0)}</dd></div><div><dt>判定有效至</dt><dd>${escape(date(assessment.valid_until_epoch_ms))}</dd></div></dl>`;
      const reasons = (assessment.reasons || []).map(text);
      if (reasons.length) html += `<ul class="feedback-reasons">${reasons.map(r => `<li>${escape(r)}</li>`).join("")}</ul>`;
      const metrics = assessment.metrics || [];
      if (metrics.length) html += table(["指标", "真实样本 / 到期点", "完整率", "原 / 新超出率", "预留变化", "未通过项"], metrics.map(m => [metric(resource,m.metric,m.container), `${number(m.matched_targets,0)} / ${number(m.due_targets,0)}`, percent(m.observation_coverage), `${percent(m.baseline_rate)} / ${percent(m.shadow_rate)}`, m.reservation_reduction == null ? "—" : `${m.reservation_reduction >= 0 ? "减少" : "增加"} ${percent(Math.abs(m.reservation_reduction))}`, (m.failed_checks || []).map(text).join("；") || "通过"]));
      if (assessment.stability) html += notice(`建议变更率：原策略 ${percent(assessment.stability.baseline_change_rate)}，校准方案 ${percent(assessment.stability.candidate_change_rate)}；基于 ${number(assessment.stability.transitions,0)} 次可比转换。`);
    }
    const rules = payload.rules || {};
    html += `<details class="feedback-rules"><summary>判定规则与边界</summary>${notice(`至少 ${rules.min_paired_runs ?? 12} 轮连续对照、每指标 ${rules.min_matched_targets ?? 100} 个真实目标、覆盖 ${rules.min_span_hours ?? 72} 小时，观测完整率至少 ${percent(rules.min_observation_coverage ?? 0.95)}。风险和建议变更不得增加，至少一个预留维度减少 ${percent(rules.min_reservation_reduction ?? 0.05)}。`)}${notice("评审通过不等于已启用。过期报告、规格变化或新数据可能使结论失效；正式执行还需重新核验。")}</details></section>`;
    host.innerHTML = html;
  }

  async function load(resource) {
    const current = ++requestId;
    host.innerHTML = notice("正在读取校准与验证结果…");
    try {
      const payload = await window.ResourceApi.requestJson(`/api/resources/${encodeURIComponent(resource.resource_id)}/feedback`);
      if (current === requestId && window.ResourcePredictApp.state.selectedResourceId === resource.resource_id) render(resource,payload);
    } catch (error) {
      if (current !== requestId || window.ResourcePredictApp.state.selectedResourceId !== resource.resource_id) return;
      render(resource,{ report_status:"error" });
      const button = document.createElement("button");
      button.className = "icon-text-btn";
      button.textContent = "重新读取反馈";
      button.onclick = () => load(resource);
      host.append(button);
    }
  }
  window.ResourceFeedback = { load };
})();
