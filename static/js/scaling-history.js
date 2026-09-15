(function () {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const statuses = {queued:"排队中", running:"执行中", waiting_confirm:"等待确认", confirming:"确认中", success:"成功", failed:"失败"};
  const fields = {replicas:"副本", cpu_cores:"CPU（核）", memory_gb:"内存（GiB）", disk_gb:"磁盘（GB）", flavor:"规格型号",
    cpu_request_cores:"CPU Request（核）", cpu_limit_cores:"CPU Limit（核）", memory_request_gb:"内存 Request（GiB）", memory_limit_gb:"内存 Limit（GiB）"};
  const date = value => value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "未记录";
  let page = 1, pages = 1, sequence = 0, requestJson, initialized = false, query = "";

  function specs(spec) {
    if (!spec || typeof spec !== "object") return {};
    const result = {};
    for (const [key, label] of Object.entries(fields)) {
      const value = key === "replicas" ? spec.replicas ?? spec.replicas_observed ?? spec.current_replicas : spec[key];
      if (value != null) result[label] = value;
    }
    for (const [name, values] of Object.entries(spec.containers || {})) {
      for (const [key, label] of Object.entries(fields)) {
        if (values?.[key] != null) result[`${name} / ${label}`] = values[key];
      }
    }
    return result;
  }

  function comparison(task) {
    const before = specs(task.before_spec);
    const target = JSON.parse(JSON.stringify(task.effective_spec && Object.keys(task.effective_spec).length ? task.effective_spec : task.target_spec || {}));
    // 单容器旧目标可能使用平铺字段，按已冻结的容器名展示。
    const containers = Object.keys(task.before_spec?.containers || {});
    if (containers.length === 1 && !target.containers) {
      for (const key of ["cpu_request_cores", "cpu_limit_cores", "memory_request_gb", "memory_limit_gb"]) {
        if (target[key] != null) {
          target.containers ||= {[containers[0]]:{}};
          target.containers[containers[0]][key] = target[key];
          delete target[key];
        }
      }
    }
    const changes = specs(target), after = {...before, ...changes};
    const success = task.mode === "execute" && task.status === "success";
    const heading = success ? "调配后规格（命令结果）" : "目标规格（未确认完成）";
    const keys = [...new Set([...Object.keys(before), ...Object.keys(changes)])];
    if (!keys.length) return '<p class="history-note">规格未记录</p>';
    return `<div class="history-specs"><table><thead><tr><th>配置项</th><th>调配前规格</th><th>${heading}</th></tr></thead><tbody>${keys.map(key => {
      const changed = key in changes && String(before[key]) !== String(changes[key]);
      return `<tr${changed ? ' class="is-changed"' : ""}><th>${escape(key)}</th><td>${escape(before[key] ?? "未记录")}</td><td>${escape(after[key] ?? "未记录")}${changed ? '<span class="history-change">变更</span>' : ""}</td></tr>`;
    }).join("")}</tbody></table></div>`;
  }

  function render(items) {
    if (!items.length) return '<div class="empty-list is-compact">没有符合条件的调配记录。</div>';
    return items.map(task => {
      const status = statuses[task.status] || "未知状态";
      const mode = task.mode === "dry_run" ? "预检" : "调配";
      return `<article class="history-record">
        <header><div><strong>${escape(task.resource_id || "未知资源")}</strong><small>${escape(task.cluster || "")} · ${escape(task.task_id)}</small></div><span class="task-status">${mode} · ${status}</span></header>
        <div class="history-meta"><span>提交时间：${escape(date(task.created_at_ms))}</span><span>完成时间：${escape(date(task.finished_at_ms))}</span><span>操作人：${escape(task.operator || "未记录")}</span></div>
        ${comparison(task)}
        ${task.error ? `<p class="history-error">失败原因：${escape(task.error)}</p>` : ""}
        ${task.snapshot_error ? `<p class="history-error">本地规格同步异常：${escape(task.snapshot_error)}</p>` : ""}
      </article>`;
    }).join("");
  }

  async function load(request) {
    requestJson = request || requestJson;
    const host = document.getElementById("task-history");
    if (!host) return;
    if (!initialized) {
      document.getElementById("task-history-search").onsubmit = event => {
        event.preventDefault(); query = document.getElementById("task-history-query").value.trim(); page = 1; load();
      };
      document.getElementById("task-history-prev").onclick = () => { if (page > 1) { page--; load(); } };
      document.getElementById("task-history-next").onclick = () => { if (page < pages) { page++; load(); } };
      initialized = true;
    }
    const current = ++sequence;
    const prev = document.getElementById("task-history-prev"), next = document.getElementById("task-history-next");
    prev.disabled = next.disabled = true;
    host.innerHTML = '<div class="empty-list is-compact">正在读取全部资源调配记录…</div>';
    try {
      const result = await requestJson(`/api/scaling-history?page=${page}&page_size=20&q=${encodeURIComponent(query)}`, 1);
      if (current !== sequence) return;
      page = result.page; pages = Math.max(1, Math.ceil(result.total / result.page_size));
      host.innerHTML = render(result.items || []);
      document.getElementById("task-history-total").textContent = `共 ${result.total} 条记录`;
      document.getElementById("task-history-page").textContent = `第 ${page} / ${pages} 页`;
      prev.disabled = page <= 1; next.disabled = page >= pages;
    } catch (error) {
      if (current !== sequence) return;
      host.innerHTML = `<div class="history-error">调配记录读取失败：${escape(error.message || error)}，请点击查询 / 刷新重试。</div>`;
    }
  }
  window.ScalingHistory = {load, render};
})();
