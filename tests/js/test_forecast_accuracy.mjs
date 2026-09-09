import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const window = {};
vm.runInNewContext(fs.readFileSync("static/js/forecast-accuracy.js", "utf8"), { window, URLSearchParams });
const accuracy = window.ForecastAccuracy;

test("missing values are not zero and rates are fractions", () => {
  assert.equal(accuracy.number(null), "—");
  assert.equal(accuracy.number(undefined), "—");
  assert.equal(accuracy.number(Infinity), "—");
  assert.equal(accuracy.number(""), "—");
  assert.equal(accuracy.number(0), "0");
  assert.equal(accuracy.rate(0), "0%");
  assert.equal(accuracy.rate(0.95), "95%");
  const html = accuracy.renderSummary({ coverage: {}, summary: [{ unit: "percentage_points", mae: 5, hit_rate_5pp: 1 }] });
  assert.match(html, /5 pp（百分点）/);
  assert.match(html, /100% \/ —/);
  assert.match(html, /含边界/);
  assert.match(html, /等权/);
});

test("summary and point tables escape untrusted text and preserve status", () => {
  const row = { resource_id: '<img src=x onerror="evil()">', model: "<script>", unit: "percentage_points", predicted: 0, actual: null, error: null, hit_5pp: null, hit_10pp: null, status: "awaiting_observation" };
  const html = accuracy.renderItems({ items: [row] });
  assert.doesNotMatch(html, /<img|<script>/);
  assert.match(html, /&lt;img/);
  assert.match(html, /缺少实测 \/ — \/ —/);
  assert.match(html, /<td>0 %<\/td><td>— %/);
  const abs = accuracy.renderSummary({ summary: [{ unit: "cores", mae: .1, hit_rate_5pp: null }] });
  assert.match(abs, /0.1 cores/);
});

test("source, layer, model, horizon and target range persist identically in exports", () => {
  assert.equal(new URLSearchParams(accuracy.query()).get("source"), "realized");
  assert.equal(new URLSearchParams(accuracy.query()).get("level"), "resource");
  const filters = { source: "holdout", resource_type: "k8s_workload", level: "container", metric: "cpu", model: "a&b", horizon: ">24h", q: "中文 / a", from_local: "2026-09-01T00:00", to_local: "2026-09-02T00:00", page: 2, page_size: 50 };
  const full = new URLSearchParams(accuracy.query(filters));
  const exported = new URLSearchParams(accuracy.query(filters, false));
  for (const [key, value] of exported) assert.equal(full.get(key), value);
  assert.equal(exported.get("model"), "a&b");
  assert.equal(exported.get("horizon"), ">24h");
  assert.equal(exported.has("page"), false);
  assert.equal(exported.has("page_size"), false);
  assert.ok(Number(exported.get("from_ms")) < Number(exported.get("to_ms")));
  assert.throws(() => accuracy.query({ from_local: "2026-02-31T00:00" }), /有效/);
  assert.throws(() => accuracy.query({ from_local: "2026-09-02T00:00", to_local: "2026-09-01T00:00" }), /晚于/);
});

test("legacy never invents paired truth or curves", () => {
  const payload = { source: "legacy", coverage: {}, items: [{ resource_id: "old", mae: 0.02, mape: 5 }] };
  const summary = accuracy.renderSummary(payload);
  const rows = accuracy.renderItems(payload);
  assert.match(summary, /不能追认为独立测试或预测兑现/);
  assert.match(rows, /原始 MAPE/);
  assert.match(rows, /无逐点预测\/实际证据/);
  assert.doesNotMatch(rows, /查看对照|95%/);
});

test("chart cohorts never mix resources, batches or denominators", () => {
  const selected = { resource_id: "a", resource_type: "k8s_workload", container: "x", metric: "cpu", model: "m", batch: "1", unit: "percentage_points", basis_unit: "request", target_ms: 2 };
  const rows = [selected, { ...selected, target_ms: 1 }, { ...selected, batch: "2" }, { ...selected, basis_unit: "limit" }, { ...selected, resource_id: "b" }];
  const result = accuracy.curvePoints(rows, selected);
  assert.equal(result.length, 2);
  assert.equal(result[0].target_ms, 1);
});

test("snapshot completion links only a safe completed local package and retains filters", () => {
  const payload = { snapshot_id: "abc", download_url: "/api/forecast-accuracy/snapshots/abc/download", sha256: "hash", point_count: 0, record_count: 2 };
  const html = accuracy.renderSnapshot(payload, "source=legacy&model=a%26b");
  assert.match(html, /0 个证据点 · 2 条记录/);
  assert.match(html, /source=legacy&amp;model=a%26b/);
  assert.match(html, /不包含可核验预测\/实际配对证据/);
  assert.match(html, /SHA256 hash/);
  assert.throws(() => accuracy.renderSnapshot({ ...payload, download_url: "javascript:alert(1)" }, ""), /无效/);
});

test("load failure leaves explicit error and disabled snapshot", async () => {
  const nodes = new Map();
  const element = () => ({ innerHTML: "", hidden: false, disabled: false, addEventListener() {} });
  const host = { ...element(), querySelector(selector) { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); } };
  const isolated = { addEventListener() {}, ResourceApi: { requestJson: async () => { throw new Error("<failed>"); } } };
  vm.runInNewContext(fs.readFileSync("static/js/forecast-accuracy.js", "utf8"), { window: isolated, document: { getElementById: () => host }, URLSearchParams });
  await isolated.ForecastAccuracy.load();
  assert.match(nodes.get("#accuracy-result").innerHTML, /评估加载失败：&lt;failed&gt;/);
  assert.equal(nodes.get("#accuracy-snapshot").disabled, true);
});
