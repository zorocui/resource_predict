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
        const payload = await r.json().catch(() => ({}));
        lastErr = buildHttpError(r, payload);
        if (r.status < 500) break;
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr;
  }

  function buildHttpError(response, payload) {
    const message = payload?.error || `请求失败: ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload || {};
    error.updateStatus = payload?.status || null;
    return error;
  }

  async function postJson(url, body, method = "POST") {
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const payload = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw buildHttpError(r, payload);
    }
    return payload;
  }

  window.ResourceApi = { buildQuery, requestJson, postJson };
})();
