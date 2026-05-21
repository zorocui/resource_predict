(function () {
  const modalEl = document.getElementById("scaling-modal");
  const titleEl = document.getElementById("scaling-modal-title");
  const subtitleEl = document.getElementById("scaling-modal-subtitle");
  const progressTrackEl = document.getElementById("scaling-progress-track");
  const progressListEl = document.getElementById("scaling-progress-list");
  const logViewEl = document.getElementById("scaling-log-view");
  let activeRow = null;
  let activeRequestJson = null;
  let activePostJson = null;

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function buildControls(resourceId, action, confidence, hasMixed) {
    const disabled = action === "hold";
    const disabledAttr = disabled ? " disabled" : "";
    const title = disabled ? "建议保持，无需调配" : "后台将在控制节点生成或执行调配命令";
    const risky = confidence === "low" || hasMixed;
    return `
      <div class="scaling-controls" data-scaling-resource="${escapeHtml(resourceId)}">
        <button type="button" class="scale-btn scale-preview" data-scaling-mode="dry_run"${disabledAttr} title="${escapeHtml(title)}">预检</button>
        <button type="button" class="scale-btn scale-execute${risky ? " is-risky" : ""}" data-scaling-mode="execute"${disabledAttr} title="${escapeHtml(title)}">调配</button>
        <span class="scaling-status" data-role="scaling-status"></span>
      </div>`;
  }

  function summarizeTask(task) {
    const status = String(task?.status || "");
    const mode = String(task?.mode || "");
    const phase = String(task?.phase || "");
    const commandIndex = Number(task?.command_index || 0);
    const commandTotal = Number(task?.command_total || 0);
    if (status === "queued") return "Queued...";
    if (status === "running") {
      if (phase === "loading_config") return "Loading cluster config...";
      if (phase === "plan_built") return "Plan generated";
      if (phase === "executing_command") return commandTotal > 0 ? `Executing command ${commandIndex}/${commandTotal}...` : "Executing command...";
      if (phase === "command_finished") return "Command finished, collecting result...";
      if (phase === "updating_snapshot") return "Syncing local spec and advice...";
      return mode === "dry_run" ? "Dry run..." : "Scaling...";
    }
    if (status === "waiting_confirm") return "Waiting for manual confirm";
    if (status === "confirming") {
      if (phase === "executing_confirm") return "Executing resize confirm...";
      if (phase === "confirm_command_finished") return "Confirm command finished...";
      if (phase === "updating_snapshot") return "Syncing local spec and advice...";
      return "Confirming resize...";
    }
    if (status === "success") return mode === "dry_run" ? "Dry run passed" : "Scaling completed";
    if (status === "failed") return `Failed: ${String(task?.error || "unknown error").slice(0, 80)}`;
    return status || "-";
  }

  function taskTitle(task) {
    const plan = task?.plan || {};
    const commands = Array.isArray(plan.commands) ? plan.commands : [];
    const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
    const lines = [];
    if (commands.length) {
      lines.push("命令:");
      commands.forEach((cmd) => lines.push(cmd));
    }
    if (warnings.length) {
      lines.push("提示:");
      warnings.forEach((x) => lines.push(x));
    }
    if (task?.error) lines.push(`错误: ${task.error}`);
    if (task.local_update) {
      const u = task.local_update || {};
      lines.push("", "local snapshot:");
      if (u.error) {
        lines.push(`error: ${String(u.error).slice(0, 1200)}`);
      } else {
        lines.push(`summary=${!!u.summary_updated} detail=${!!u.detail_updated} raw=${!!u.raw_updated} manifest=${!!u.manifest_updated}`);
        lines.push(`advice_recomputed=${!!u.advice_recomputed}`);
      }
    }
    return lines.join("\n");
  }

  function stepsFor(task, phase) {
    const status = String(task?.status || phase || "submitting");
    const mode = String(task?.mode || "");
    const taskPhase = String(task?.phase || "");
    const hasPlan = !!task?.plan;
    const waitingConfirm = status === "waiting_confirm";
    const confirming = status === "confirming";
    const failed = status === "failed";
    const success = status === "success";
    const commandNote = taskPhase === "executing_command"
      ? `Executing command ${Number(task?.command_index || 0)}/${Number(task?.command_total || 0)}`
      : (taskPhase === "command_finished" ? "Command returned; collecting result" : (mode === "dry_run" ? "Dry run does not execute commands" : "Executing command on control node"));
    const steps = [
      { name: "Submit", note: "Task submitted to backend" },
      { name: "Plan", note: hasPlan ? "Scaling commands generated" : "Reading resource, cluster, and target spec" },
      { name: mode === "dry_run" ? "Dry Run" : "Execute", note: commandNote },
      { name: "Confirm", note: waitingConfirm ? "OpenStack resized; waiting for page confirm" : (confirming ? "Running resize confirm" : "Auto-confirmed or no manual confirm needed") },
      { name: "Done", note: summarizeTask(task) },
    ];
    let activeIndex = 0;
    if (status === "running" && !hasPlan) activeIndex = 1;
    else if (status === "running" && taskPhase === "updating_snapshot") activeIndex = 4;
    else if (status === "running" && hasPlan) activeIndex = 2;
    else if (waitingConfirm || confirming) activeIndex = 3;
    else if (success || failed) activeIndex = 4;
    return steps.map((step, idx) => ({
      ...step,
      state: failed && idx === activeIndex ? "failed" : (idx < activeIndex || success ? "done" : (idx === activeIndex ? "active" : "pending")),
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
    if (taskPhase === "executing_command") {
      const total = Math.max(1, Number(task?.command_total || 1));
      const idx = Math.max(0, Number(task?.command_index || 0) - 1);
      return Math.min(78, 56 + Math.round((idx / total) * 20));
    }
    if (taskPhase === "command_finished") return 78;
    if (taskPhase === "updating_snapshot") return 88;
    if (status === "running" && task?.plan) return 72;
    if (status === "running") return 46;
    if (status === "queued") return 24;
    return 12;
  }

  function formatLog(task) {
    if (!task) return "正在提交任务...";
    const plan = task.plan || {};
    const details = plan.details || {};
    const lines = [
      `任务ID: ${task.task_id || "-"}`,
      `资源: ${task.resource_id || "-"}`,
      `模式: ${task.mode || "-"}`,
      `状态: ${task.status || "-"}`,
    ];
    if (plan.resource_type) lines.push(`资源类型: ${plan.resource_type}`);
    if (plan.cluster) lines.push(`集群: ${plan.cluster}`);
    if (plan.action) lines.push(`动作: ${plan.action}`);
    if (details.selected_flavor) {
      const f = details.selected_flavor;
      lines.push(`选择Flavor: ${f.name || "-"} (${f.source || "remote"})`);
      if (f.cpu_cores || f.memory_gb || f.disk_gb) {
        lines.push(`Flavor规格: ${f.cpu_cores || "-"}C / ${f.memory_gb || "-"}G / ${f.disk_gb || "-"}G`);
      }
    }
    const commands = Array.isArray(plan.commands) ? plan.commands : [];
    if (commands.length) {
      lines.push("", "命令:");
      commands.forEach((cmd, idx) => lines.push(`${idx + 1}. ${cmd}`));
    }
    const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
    if (warnings.length) {
      lines.push("", "提示:");
      warnings.forEach((x) => lines.push(`- ${x}`));
    }
    const results = Array.isArray(task.results) ? task.results : [];
    if (results.length) {
      lines.push("", "执行结果:");
      results.forEach((r, idx) => {
        lines.push(`${idx + 1}. exit_code=${r.exit_code} duration=${r.duration_seconds || "-"}s`);
        if (r.stdout) lines.push(`stdout: ${String(r.stdout).slice(0, 1200)}`);
        if (r.stderr) lines.push(`stderr: ${String(r.stderr).slice(0, 1200)}`);
      });
    }
    if (task.local_update) {
      const u = task.local_update || {};
      lines.push("", "local snapshot:");
      if (u.error) {
        lines.push(`error: ${String(u.error).slice(0, 1200)}`);
      } else {
        lines.push(`summary=${!!u.summary_updated} detail=${!!u.detail_updated} raw=${!!u.raw_updated} manifest=${!!u.manifest_updated}`);
        lines.push(`advice_recomputed=${!!u.advice_recomputed}`);
      }
    }
    if (task.error) lines.push("", `错误: ${task.error}`);
    return lines.join("\n");
  }

  function renderModal(task, phase) {
    if (!modalEl) return;
    const status = String(task?.status || phase || "submitting");
    const mode = String(task?.mode || "");
    if (titleEl) titleEl.textContent = mode === "execute" ? "调配进度" : "预检进度";
    if (subtitleEl) subtitleEl.textContent = `${task?.resource_id || ""} ${summarizeTask(task || { status })}`.trim();
    if (progressTrackEl) {
      progressTrackEl.style.setProperty("--progress", `${progressPercent(task, phase)}%`);
      progressTrackEl.classList.toggle("is-success", status === "success");
      progressTrackEl.classList.toggle("is-failed", status === "failed");
    }
    if (progressListEl) {
      const confirmButton = status === "waiting_confirm" && task?.task_id
        ? `<button type="button" class="scale-btn scale-execute scaling-confirm-btn" data-scaling-confirm-task="${escapeHtml(task.task_id)}">确认生效</button>`
        : "";
      progressListEl.innerHTML = stepsFor(task, phase).map((step) => (
        `<div class="scaling-step is-${step.state}">` +
        `<span class="scaling-step-dot"></span>` +
        `<div><div class="scaling-step-name">${escapeHtml(step.name)}</div>` +
        `<div class="scaling-step-note">${escapeHtml(step.note)}</div></div>` +
        `</div>`
      )).join("") + confirmButton;
    }
    if (logViewEl) logViewEl.textContent = formatLog(task);
    modalEl.hidden = false;
  }

  function closeModal() {
    if (modalEl) modalEl.hidden = true;
  }

  async function pollTask(taskId, row, deps) {
    const requestJson = deps?.requestJson;
    const statusEl = row?.querySelector('[data-role="scaling-status"]');
    if (!statusEl) return;
    for (let i = 0; i < 90; i++) {
      try {
        const payload = await requestJson(`/api/scaling-tasks/${encodeURIComponent(taskId)}`, 1);
        const task = payload.task || {};
        const status = String(task.status || "");
        renderModal(task);
        statusEl.textContent = summarizeTask(task);
        statusEl.title = taskTitle(task);
        statusEl.classList.toggle("is-success", status === "success");
        statusEl.classList.toggle("is-failed", status === "failed");
        if (status !== "queued" && status !== "running" && status !== "confirming") {
          row.querySelectorAll("[data-scaling-mode]").forEach((btn) => {
            btn.disabled = status === "waiting_confirm";
          });
          if (status === "success") {
            window.dispatchEvent(new CustomEvent("resource-scaled", {
              detail: { resourceId: task.resource_id || row?.dataset.resourceId || "", task },
            }));
          }
          return;
        }
      } catch (e) {
        renderModal({
          task_id: taskId,
          resource_id: row?.dataset.resourceId || "",
          status: "failed",
          error: String(e),
        });
        statusEl.textContent = `查询失败：${String(e).slice(0, 80)}`;
        statusEl.classList.add("is-failed");
        row.querySelectorAll("[data-scaling-mode]").forEach((btn) => {
          btn.disabled = false;
        });
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    statusEl.textContent = "仍在执行，请稍后查看";
    renderModal({
      task_id: taskId,
      resource_id: row?.dataset.resourceId || "",
      status: "running",
      error: "仍在执行，请稍后查看",
    });
  }

  async function start(btn, deps) {
    const requestJson = deps?.requestJson;
    const postJson = deps?.postJson;
    if (typeof requestJson !== "function" || typeof postJson !== "function") {
      throw new Error("ScalingUI requires requestJson and postJson");
    }
    const row = btn.closest(".row");
    activeRow = row;
    activeRequestJson = requestJson;
    activePostJson = postJson;
    const resourceId = row?.dataset.resourceId || "";
    const mode = btn.dataset.scalingMode || "dry_run";
    if (!row || !resourceId) return;
    let confirmCreateFlavor = mode === "dry_run";
    if (mode === "execute") {
      const ok = window.confirm("确认执行调配命令？后台会登录该资源所在集群的控制节点并修改云资源规格。");
      if (!ok) return;
      confirmCreateFlavor = window.confirm(
        "如果 OpenStack 集群中没有符合目标规格的 flavor，是否允许后台先创建新 flavor 再调配？\n\n" +
        "选择“确定”：允许创建新 flavor。\n" +
        "选择“取消”：不创建 flavor；若没有合适 flavor，本次调配会失败。"
      );
    }
    const statusEl = row.querySelector('[data-role="scaling-status"]');
    row.querySelectorAll("[data-scaling-mode]").forEach((x) => {
      x.disabled = true;
    });
    if (statusEl) {
      statusEl.textContent = mode === "dry_run" ? "提交预检..." : "提交调配...";
      statusEl.title = "";
      statusEl.classList.remove("is-success", "is-failed");
    }
    renderModal({ resource_id: resourceId, mode, status: "submitting" }, "submitting");
    try {
      const payload = await postJson(`/api/resources/${encodeURIComponent(resourceId)}/scale`, {
        mode,
        confirm: mode === "execute",
        confirm_create_flavor: confirmCreateFlavor,
      });
      const taskId = payload.task_id || payload.task?.task_id;
      if (!taskId) throw new Error("后台未返回 task_id");
      renderModal(payload.task || { task_id: taskId, resource_id: resourceId, mode, status: "queued" });
      await pollTask(taskId, row, { requestJson, postJson });
    } catch (e) {
      renderModal({
        resource_id: resourceId,
        mode,
        status: "failed",
        error: String(e.message || e),
      });
      if (statusEl) {
        statusEl.textContent = `失败：${String(e.message || e).slice(0, 80)}`;
        statusEl.classList.add("is-failed");
      }
      row.querySelectorAll("[data-scaling-mode]").forEach((x) => {
        x.disabled = false;
      });
    }
  }

  modalEl?.addEventListener("click", (e) => {
    const confirmBtn = e.target.closest("[data-scaling-confirm-task]");
    if (confirmBtn) {
      e.preventDefault();
      const taskId = confirmBtn.dataset.scalingConfirmTask || "";
      if (!taskId || typeof activePostJson !== "function" || typeof activeRequestJson !== "function") return;
      const ok = window.confirm("确认执行 OpenStack resize confirm？确认后本次规格调整会正式生效，并同步更新本地规格和调配建议。");
      if (!ok) return;
      confirmBtn.disabled = true;
      activePostJson(`/api/scaling-tasks/${encodeURIComponent(taskId)}/confirm`, { confirm: true })
        .then((payload) => {
          renderModal(payload.task || { task_id: taskId, status: "confirming" });
          return pollTask(taskId, activeRow, { requestJson: activeRequestJson, postJson: activePostJson });
        })
        .catch((err) => {
          renderModal({ task_id: taskId, status: "waiting_confirm", error: String(err.message || err) });
        });
      return;
    }
    if (e.target.closest("[data-scaling-modal-dismiss]")) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modalEl && !modalEl.hidden) closeModal();
  });

  window.ScalingUI = {
    buildControls,
    closeModal,
    start,
  };
}());
