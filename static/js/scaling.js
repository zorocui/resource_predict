(function () {
  const modalEl = document.getElementById("scaling-modal");
  const titleEl = document.getElementById("scaling-modal-title");
  const subtitleEl = document.getElementById("scaling-modal-subtitle");
  const progressTrackEl = document.getElementById("scaling-progress-track");
  const progressListEl = document.getElementById("scaling-progress-list");
  const logViewEl = document.getElementById("scaling-log-view");
  let activeContainer = null;
  let activeRequestJson = null;
  let activePostJson = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function summarizeTask(task) {
    const status = String(task?.status || "");
    const mode = String(task?.mode || "");
    const phase = String(task?.phase || "");
    if (status === "queued") return "已进入队列";
    if (status === "running") {
      if (phase === "loading_config") return "正在读取集群配置";
      if (phase === "plan_built") return "调配计划已生成";
      if (phase === "executing_command") return `正在执行命令 ${Number(task?.command_index || 0)}/${Number(task?.command_total || 0)}`;
      if (phase === "updating_snapshot") return "正在同步本地快照";
      return mode === "dry_run" ? "正在预检" : "正在调配";
    }
    if (status === "waiting_confirm") return "等待确认生效";
    if (status === "confirming") return "正在确认 resize";
    if (status === "success") return mode === "dry_run" ? "预检通过" : "调配完成";
    if (status === "failed") return `失败：${String(task?.error || "未知错误").slice(0, 80)}`;
    return status || "-";
  }

  function stepsFor(task, phase) {
    const status = String(task?.status || phase || "submitting");
    const mode = String(task?.mode || "");
    const taskPhase = String(task?.phase || "");
    const labels = [
      ["提交", "任务已提交到后端"],
      ["计划", task?.plan ? "调配命令已生成" : "读取资源和目标规格"],
      [mode === "dry_run" ? "预检" : "执行", taskPhase === "executing_command" ? summarizeTask(task) : "在控制节点生成或执行命令"],
      ["确认", status === "waiting_confirm" ? "等待手动确认" : "无需确认或已确认"],
      ["完成", summarizeTask(task)],
    ];
    let active = 0;
    if (status === "running" && !task?.plan) active = 1;
    else if (status === "running" && taskPhase === "updating_snapshot") active = 4;
    else if (status === "running" && task?.plan) active = 2;
    else if (status === "waiting_confirm" || status === "confirming") active = 3;
    else if (status === "success" || status === "failed") active = 4;
    return labels.map(([name, note], idx) => ({
      name,
      note,
      state: status === "failed" && idx === active ? "failed" : (idx < active || status === "success" ? "done" : (idx === active ? "active" : "pending")),
    }));
  }

  function progressPercent(task, phase) {
    const status = String(task?.status || phase || "submitting");
    const taskPhase = String(task?.phase || "");
    if (status === "success" || status === "failed") return 100;
    if (status === "waiting_confirm") return 84;
    if (status === "confirming") return 92;
    if (taskPhase === "loading_config") return 32;
    if (taskPhase === "plan_built") return 48;
    if (taskPhase === "executing_command") return 68;
    if (taskPhase === "updating_snapshot") return 88;
    if (status === "running") return 46;
    if (status === "queued") return 24;
    return 12;
  }

  function formatLog(task) {
    if (!task) return "正在提交任务...";
    const plan = task.plan || {};
    const lines = [
      `任务 ID: ${task.task_id || "-"}`,
      `资源: ${task.resource_id || "-"}`,
      `模式: ${task.mode || "-"}`,
      `状态: ${task.status || "-"}`,
    ];
    if (plan.resource_type) lines.push(`资源类型: ${plan.resource_type}`);
    if (plan.cluster) lines.push(`集群: ${plan.cluster}`);
    if (plan.action) lines.push(`动作: ${plan.action}`);
    if (Array.isArray(plan.commands) && plan.commands.length) {
      lines.push("", "命令:");
      plan.commands.forEach((cmd, idx) => lines.push(`${idx + 1}. ${cmd}`));
    }
    if (Array.isArray(plan.warnings) && plan.warnings.length) {
      lines.push("", "提示:");
      plan.warnings.forEach((x) => lines.push(`- ${x}`));
    }
    if (Array.isArray(task.results) && task.results.length) {
      lines.push("", "执行结果:");
      task.results.forEach((r, idx) => {
        lines.push(`${idx + 1}. exit_code=${r.exit_code} duration=${r.duration_seconds || "-"}s`);
        if (r.stdout) lines.push(`stdout: ${String(r.stdout).slice(0, 1200)}`);
        if (r.stderr) lines.push(`stderr: ${String(r.stderr).slice(0, 1200)}`);
      });
    }
    if (task.error) lines.push("", `错误: ${task.error}`);
    return lines.join("\n");
  }

  function renderModal(task, phase) {
    const status = String(task?.status || phase || "submitting");
    const mode = String(task?.mode || "");
    titleEl.textContent = mode === "execute" ? "调配进度" : "预检进度";
    subtitleEl.textContent = `${task?.resource_id || ""} ${summarizeTask(task || { status })}`.trim();
    progressTrackEl.style.setProperty("--progress", `${progressPercent(task, phase)}%`);
    progressTrackEl.classList.toggle("is-success", status === "success");
    progressTrackEl.classList.toggle("is-failed", status === "failed");
    const confirmButton = status === "waiting_confirm" && task?.task_id
      ? `<button type="button" class="scale-btn scale-execute scaling-confirm-btn" data-scaling-confirm-task="${escapeHtml(task.task_id)}">确认生效</button>`
      : "";
    progressListEl.innerHTML = stepsFor(task, phase).map((step) => (
      `<div class="scaling-step is-${step.state}"><span class="scaling-step-dot"></span><div><div class="scaling-step-name">${escapeHtml(step.name)}</div><div class="scaling-step-note">${escapeHtml(step.note)}</div></div></div>`
    )).join("") + confirmButton;
    logViewEl.textContent = formatLog(task);
    modalEl.hidden = false;
  }

  function closeModal() {
    modalEl.hidden = true;
  }

  function setControlsBusy(container) {
    container?.querySelectorAll("[data-scaling-mode]").forEach((btn) => {
      btn.dataset.scalingWasDisabled = btn.disabled ? "true" : "false";
      btn.disabled = true;
    });
  }

  function restoreControls(container) {
    container?.querySelectorAll("[data-scaling-mode]").forEach((btn) => {
      if (btn.dataset.scalingWasDisabled === undefined) return;
      btn.disabled = btn.dataset.scalingWasDisabled === "true";
      delete btn.dataset.scalingWasDisabled;
    });
  }

  async function pollTask(taskId, container, deps) {
    const statusEl = container?.querySelector('[data-role="scaling-status"], [data-role="manual-scaling-status"]');
    for (let i = 0; i < 90; i++) {
      try {
        const payload = await deps.requestJson(`/api/scaling-tasks/${encodeURIComponent(taskId)}`, 1);
        const task = payload.task || {};
        const status = String(task.status || "");
        renderModal(task);
        if (statusEl) {
          statusEl.textContent = summarizeTask(task);
          statusEl.classList.toggle("is-success", status === "success");
          statusEl.classList.toggle("is-failed", status === "failed");
        }
        if (status !== "queued" && status !== "running" && status !== "confirming") {
          if (status === "waiting_confirm") setControlsBusy(container);
          else restoreControls(container);
          if (status === "success") {
            window.dispatchEvent(new CustomEvent("resource-scaled", { detail: { resourceId: task.resource_id || "", task } }));
          }
          return;
        }
      } catch (e) {
        renderModal({ task_id: taskId, status: "failed", error: String(e.message || e) });
        if (statusEl) {
          statusEl.textContent = `查询失败：${String(e.message || e).slice(0, 80)}`;
          statusEl.classList.add("is-failed");
        }
        restoreControls(container);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    restoreControls(container);
  }

  async function start(btn, deps) {
    const container = btn.closest("[data-scaling-resource]");
    activeContainer = container;
    activeRequestJson = deps.requestJson;
    activePostJson = deps.postJson;
    const resourceId = container?.dataset.scalingResource || "";
    const resourceType = container?.dataset.scalingResourceType || "";
    const mode = btn.dataset.scalingMode || "dry_run";
    const source = btn.dataset.scalingSource || "suggested";
    if (!container || !resourceId) return;
    const targetSpec = source === "manual" ? collectManualTargetSpec(container) : null;
    if (source === "manual" && !targetSpec) return;
    let confirmCreateFlavor = mode === "dry_run";
    if (mode === "execute") {
      const confirmText = source === "confirmed"
        ? "确认按当前建议执行调配？该建议未达到自动执行门控，提交表示已人工复核混合信号、目标规格和风险。"
        : "确认执行调配命令？后端会登录资源所在集群的控制节点并修改资源规格。";
      const ok = window.confirm(confirmText);
      if (!ok) return;
      confirmCreateFlavor = resourceType === "k8s_workload"
        ? false
        : window.confirm("如果 OpenStack 中没有匹配目标规格的 flavor，是否允许后端创建新 flavor 后再调配？");
    }
    const statusEl = container.querySelector('[data-role="scaling-status"], [data-role="manual-scaling-status"]');
    setControlsBusy(container);
    if (statusEl) statusEl.textContent = mode === "dry_run" ? "正在提交预检..." : "正在提交调配...";
    renderModal({ resource_id: resourceId, mode, status: "submitting" }, "submitting");
    const requestBody = {
        mode,
        confirm: mode === "execute",
        confirm_create_flavor: confirmCreateFlavor,
        target_source: source,
        ...(targetSpec ? { target_spec: targetSpec } : {}),
      };
    try {
      let payload;
      try {
        payload = await deps.postJson(`/api/resources/${encodeURIComponent(resourceId)}/scale`, requestBody);
      } catch (e) {
        const message = String(e.message || e);
        const canRetryCooldown = mode === "execute" && /cooldown is active/i.test(message);
        if (!canRetryCooldown) throw e;
        const ok = window.confirm(
          `${message}\n\n资源仍处于冷却期。确认已人工复核风险，并跳过本次 cooldown 门控继续调配？`
        );
        if (!ok) throw e;
        payload = await deps.postJson(`/api/resources/${encodeURIComponent(resourceId)}/scale`, {
          ...requestBody,
          ignore_cooldown: true,
        });
      }
      const taskId = payload.task_id || payload.task?.task_id;
      if (!taskId) throw new Error("后端未返回 task_id");
      renderModal(payload.task || { task_id: taskId, resource_id: resourceId, mode, status: "queued" });
      await pollTask(taskId, container, deps);
    } catch (e) {
      renderModal({ resource_id: resourceId, mode, status: "failed", error: String(e.message || e) });
      if (statusEl) {
        statusEl.textContent = `失败：${String(e.message || e).slice(0, 80)}`;
        statusEl.classList.add("is-failed");
      }
      restoreControls(container);
    }
  }

  modalEl.addEventListener("click", (event) => {
    const confirmBtn = event.target.closest("[data-scaling-confirm-task]");
    if (confirmBtn) {
      const taskId = confirmBtn.dataset.scalingConfirmTask || "";
      if (!taskId || !activePostJson || !activeRequestJson) return;
      const ok = window.confirm("确认执行 OpenStack resize confirm？确认后本次规格调整会正式生效。");
      if (!ok) return;
      confirmBtn.disabled = true;
      activePostJson(`/api/scaling-tasks/${encodeURIComponent(taskId)}/confirm`, { confirm: true })
        .then((payload) => {
          renderModal(payload.task || { task_id: taskId, status: "confirming" });
          return pollTask(taskId, activeContainer, { requestJson: activeRequestJson, postJson: activePostJson });
        })
        .catch((err) => renderModal({ task_id: taskId, status: "waiting_confirm", error: String(err.message || err) }));
      return;
    }
    if (event.target.closest("[data-scaling-modal-dismiss]")) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modalEl.hidden) closeModal();
  });
  document.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-scaling-choice-toggle]");
    if (!toggle) return;
    const root = toggle.closest("[data-scaling-choice-root]");
    const panel = root?.querySelector("[data-scaling-choice-panel]");
    if (!panel) return;
    const nextOpen = panel.hidden;
    panel.hidden = !nextOpen;
    toggle.setAttribute("aria-expanded", nextOpen ? "true" : "false");
  });

  function collectManualTargetSpec(container) {
    const panel = container.classList.contains("manual-scaling-panel")
      ? container
      : container.parentElement?.querySelector(".manual-scaling-panel");
    const statusEl = panel?.querySelector('[data-role="manual-scaling-status"]');
    const target = { containers: {} };
    let hasValue = false;
    panel?.querySelectorAll("[data-manual-container]").forEach((row) => {
      const name = row.dataset.manualContainer || "";
      if (!name) return;
      const values = {};
      row.querySelectorAll("[data-manual-field]").forEach((input) => {
        const raw = String(input.value || "").trim();
        if (raw === "") return;
        const value = Number(raw);
        if (!Number.isFinite(value) || value < 0) return;
        values[input.dataset.manualField] = value;
      });
      if (Object.keys(values).length) {
        target.containers[name] = values;
        hasValue = true;
      }
    });
    const replicasInput = panel?.querySelector("[data-manual-replicas]");
    if (replicasInput && String(replicasInput.value || "").trim() !== "") {
      const replicas = Number(replicasInput.value);
      if (Number.isInteger(replicas) && replicas > 0) {
        target.replicas = replicas;
        hasValue = true;
      }
    }
    if (!hasValue) {
      if (statusEl) {
        statusEl.textContent = "请先填写至少一个目标规格";
        statusEl.classList.add("is-failed");
      }
      return null;
    }
    if (!Object.keys(target.containers).length) delete target.containers;
    if (statusEl) {
      statusEl.textContent = "";
      statusEl.classList.remove("is-failed");
    }
    return target;
  }

  function buildManualK8sControls(resource, resourceId, resourceType) {
    const spec = resource?.spec || {};
    const containers = containersFor(spec);
    if (!containers.length) return "";
    const kind = String(spec.workload_kind || spec.owner_kind || "").trim().toLowerCase();
    const isDaemonSet = kind === "daemonset";
    const currentReplicas = spec.replicas ?? spec.current_replicas ?? spec.replicas_observed ?? "";
    const rows = containers.map((name) => `
      <div class="manual-container-row" data-manual-container="${escapeHtml(name)}">
        <div class="manual-container-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
        ${manualInput("CPU req", "cpu_request_cores", "C")}
        ${manualInput("CPU limit", "cpu_limit_cores", "C")}
        ${manualInput("Mem req", "memory_request_gb", "GiB")}
        ${manualInput("Mem limit", "memory_limit_gb", "GiB")}
      </div>`).join("");
    const replicas = isDaemonSet
      ? `<div class="manual-note">DaemonSet 副本数由节点调度决定，这里只调整容器资源。</div>`
      : `<label class="manual-replicas"><span>控制器副本数</span><input data-manual-replicas type="number" min="1" step="1" placeholder="${escapeHtml(currentReplicas)}" /></label>`;
    return `
      <div class="manual-scaling-panel" data-scaling-resource="${escapeHtml(resourceId)}" data-scaling-resource-type="${escapeHtml(resourceType || "")}">
        <div class="manual-scaling-head"><strong>手动目标规格</strong><span>留空字段不会变更</span></div>
        <div class="manual-container-grid">${rows}</div>
        ${replicas}
        <div class="manual-scaling-actions">
          <button type="button" class="scale-btn" data-scaling-mode="dry_run" data-scaling-source="manual">手动预检</button>
          <button type="button" class="scale-btn scale-execute" data-scaling-mode="execute" data-scaling-source="manual">按手动规格调配</button>
          <span class="scaling-status" data-role="manual-scaling-status"></span>
        </div>
      </div>`;
  }

  function containersFor(spec) {
    const bySpec = spec.containers && typeof spec.containers === "object" && !Array.isArray(spec.containers)
      ? Object.keys(spec.containers).map((x) => String(x || "").trim()).filter(Boolean)
      : [];
    const observed = Array.isArray(spec.containers_observed) ? spec.containers_observed : [];
    const out = bySpec.length ? bySpec : observed.map((x) => String(x || "").trim()).filter(Boolean);
    const single = String(spec.container || "").trim();
    if (single && !out.includes(single)) out.push(single);
    return [...new Set(out)].sort();
  }

  function manualInput(label, field, unit) {
    return `<label><span>${escapeHtml(label)}</span><input data-manual-field="${escapeHtml(field)}" type="number" min="0" step="0.001" placeholder="${escapeHtml(unit)}" /></label>`;
  }

  function buildControls(resourceId, action, confidence, hasMixed, options = {}) {
    const analysisOnly = Boolean(options.analysisOnly);
    const resource = options.resource || {};
    const suggestedBlocker = suggestedTargetBlocker(options.resource || {});
    const actionGate = resource?.scaling_advice?.action_gate || {};
    const gateState = String(actionGate.state || "").toLowerCase();
    const baseDisabled = Boolean(suggestedBlocker) || analysisOnly || action === "hold" || action === "insufficient_data";
    const suggestedExecuteDisabled = baseDisabled || action === "mixed" || gateState === "observe";
    const confirmedEnabled = !baseDisabled && (action === "mixed" || gateState === "observe");
    const risky = confidence === "low" || hasMixed;
    const dryRunDisabledAttr = baseDisabled ? " disabled" : "";
    const suggestedExecuteDisabledAttr = suggestedExecuteDisabled ? " disabled" : "";
    const disabledText = suggestedBlocker
      || suggestedDisabledText(action, analysisOnly, actionGate);
    const readyText = suggestedReadyText(actionGate);
    const manualControls = options.resourceType === "k8s_workload"
      ? buildManualK8sControls(options.resource || {}, resourceId, options.resourceType)
      : "";
    const confirmedButton = confirmedEnabled
      ? `<button type="button" class="scale-btn scale-execute is-risky" data-scaling-mode="execute" data-scaling-source="confirmed">人工确认后调配</button>`
      : "";
    const suggestedStatus = (baseDisabled || suggestedExecuteDisabled)
      ? `<span class="scaling-status" data-role="scaling-status">${disabledText}</span>`
      : `<span class="scaling-status" data-role="scaling-status">${readyText}</span>`;
    return `
      <div class="scaling-choice" data-scaling-choice-root>
        <button type="button" class="scale-btn scale-primary" data-scaling-choice-toggle aria-expanded="false">调配</button>
        <div class="scaling-choice-panel" data-scaling-choice-panel hidden>
          <div class="scaling-choice-section">
            <div class="scaling-choice-head"><strong>按建议调配</strong><span>使用当前预测生成的目标规格</span></div>
            <div class="scaling-controls" data-scaling-resource="${escapeHtml(resourceId)}" data-scaling-resource-type="${escapeHtml(options.resourceType || "")}">
              <button type="button" class="scale-btn" data-scaling-mode="dry_run" data-scaling-source="suggested"${dryRunDisabledAttr}>建议预检</button>
              <button type="button" class="scale-btn scale-execute${risky ? " is-risky" : ""}" data-scaling-mode="execute" data-scaling-source="suggested"${suggestedExecuteDisabledAttr}>按建议调配</button>
              ${confirmedButton}
              ${suggestedStatus}
            </div>
          </div>
          ${manualControls ? `<div class="scaling-choice-section">${manualControls}</div>` : ""}
        </div>
      </div>`;
  }

  function suggestedDisabledText(action, analysisOnly, actionGate) {
    const gateState = String(actionGate?.state || "").toLowerCase();
    if (analysisOnly) return "当前建议缺少可执行目标，不能按建议调配";
    if (action === "mixed") return "当前建议存在混合信号，需人工确认后再调配";
    if (gateState === "observe") {
      const observed = Number(actionGate?.observed_consistent_rounds);
      const required = Number(actionGate?.required_consistent_rounds);
      if (Number.isFinite(observed) && Number.isFinite(required) && required > 0) {
        return `建议方向已连续确认 ${observed}/${required} 轮，需继续复核或人工确认后调配`;
      }
      return "当前建议需要人工确认或后续轮次复核后再调配";
    }
    if (action === "insufficient_data") return "当前数据不足，不能按建议调配";
    return "当前建议无需执行调配";
  }

  function suggestedReadyText(actionGate) {
    const observed = Number(actionGate?.observed_consistent_rounds);
    const required = Number(actionGate?.required_consistent_rounds);
    if (String(actionGate?.state || "").toLowerCase() !== "ready"
      || !Number.isFinite(observed) || !Number.isFinite(required)
      || required <= 0 || observed < required) return "";
    return `建议方向已连续确认 ${observed}/${required} 轮，轮次门控已通过`;
  }

  function suggestedTargetBlocker(resource) {
    if (!resource || String(resource.resource_type || "") !== "k8s_workload") return "";
    const spec = resource.spec || {};
    const advice = resource.scaling_advice || {};
    const target = advice.target_spec || {};
    if (!hasMultipleContainers(spec)) return "";
    if (target.containers && typeof target.containers === "object" && !Array.isArray(target.containers) && Object.keys(target.containers).length) {
      return "";
    }
    const resourceFields = [
      "cpu_request_cores",
      "cpu_limit_cores",
      "cpu_cores",
      "memory_request_gb",
      "memory_limit_gb",
      "memory_gb",
    ];
    return resourceFields.some((field) => target[field] !== undefined && target[field] !== null)
      ? "多容器 Workload 的 request/limit 建议需要指定 container；请使用手动目标规格"
      : "";
  }

  function hasMultipleContainers(spec) {
    const names = new Set();
    if (spec?.containers && typeof spec.containers === "object" && !Array.isArray(spec.containers)) {
      Object.keys(spec.containers).forEach((name) => {
        const value = String(name || "").trim();
        if (value) names.add(value);
      });
    }
    if (Array.isArray(spec?.containers_observed)) {
      spec.containers_observed.forEach((name) => {
        const value = String(name || "").trim();
        if (value) names.add(value);
      });
    }
    const single = String(spec?.container || "").trim();
    if (single) names.add(single);
    return names.size > 1;
  }

  window.ScalingUI = { buildControls, closeModal, start };
})();
