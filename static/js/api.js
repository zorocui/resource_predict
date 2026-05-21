(function () {
  function buildQuery(basePath, params) {
    const usp = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v === undefined || v === null) return;
      const s = String(v).trim();
      if (!s) return;
      usp.set(k, s);
    });
    const qs = usp.toString();
    return qs ? `${basePath}?${qs}` : basePath;
  }

  async function requestJson(url, retries = 3, initialDelay = 1000) {
    let lastErr;
    for (let attempt = 0; attempt < retries; attempt++) {
      if (attempt > 0) {
        const delay = initialDelay * Math.pow(2, attempt - 1);
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
      try {
        const r = await fetch(url);
        if (r.ok) return r.json();
        lastErr = new Error(`请求失败: ${r.status}`);
        if (r.status < 500) break;
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr;
  }

  async function postJson(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const payload = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(payload.error || `请求失败: ${r.status}`);
    }
    return payload;
  }

  window.ResourceApi = { buildQuery, requestJson, postJson };
})();
