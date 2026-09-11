import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const window = {};
vm.runInNewContext(fs.readFileSync("static/js/scaling-effects.js", "utf8"), { window, URLSearchParams });
const effects = window.ScalingEffects;

test("first collection renders snapshot comparisons without duration claims", () => {
  const metric = { metric: "cpu", basis: "capacity", unit: "cores", status: "evaluated",
    before: { observation_kind: "snapshot", utilization_pct: 25, end_ms: 100 },
    after: { observation_kind: "snapshot", utilization_pct: 50, end_ms: 200 },
    delta_pp: 25, reclaimed_capacity: 2, reclaimed_unit_hours: null };
  const html = effects.renderMetric(metric);
  assert.match(html, /首次采集对比/);
  assert.match(html, /采样时间/);
  assert.match(html, /25%/);
  assert.match(html, /50%/);
  assert.doesNotMatch(html, /前窗口|后窗口|覆盖率 \/ 有效小时|释放容量时 <strong>/);
  const event = { status: "evaluated", policy: { evaluation_mode: "next_collection" }, metrics: [metric] };
  assert.match(effects.renderProgress(event), /25% → 50%/);
  const summary = effects.renderSummary({ metrics: [{ ...metric, before_pct: 25, after_pct: 50 }] });
  assert.match(summary, /下一次有效采集/);
  assert.doesNotMatch(summary, /<th scope="col">历史窗口容量 × 小时/);
});

test("missing and zero values remain distinct; negative observations are preserved", () => {
  assert.equal(effects.number(null, "%"), "—");
  assert.equal(effects.number(undefined), "—");
  assert.equal(effects.number(0, "%"), "0%");
  assert.equal(effects.number(-12, " pp"), "-12 pp");
  const html = effects.renderMetric({ metric: "cpu", basis: "request", before: { utilization_pct: 0, coverage: 0 }, after: {}, delta_pp: -12, relative_change_pct: null }, 0);
  assert.match(html, /0%/);
  assert.match(html, /-12 pp/);
  assert.match(html, /相对变化 <strong>—/);
  assert.match(html, /覆盖率/);
});

test("provisional outcomes show measured duration and remain outside formal summary", () => {
  const metric = { metric: "cpu", basis: "request", unit: "cores", status: "provisional",
    before: { utilization_pct: 25 }, after: { utilization_pct: 50, valid_hours: 5, end_ms: 100, planned_end_ms: 200 },
    delta_pp: 25, reclaimed_capacity: 2 };
  const html = effects.renderMetric(metric, 0);
  assert.match(html, /阶段性成效/);
  assert.match(html, /有效观察 5 h/);
  assert.match(html, /尚未纳入正式汇总/);
  const progress = effects.renderProgress({ status: "provisional", metrics: [metric] });
  assert.match(progress, /25% → 50%/);
  assert.match(progress, /有效观察 5 h/);
  assert.equal(effects.renderProgress({ status: "failed", metrics: [metric] }), "");
});

test("default scope is shrink and export retains every filter but no pagination", () => {
  assert.equal(new URLSearchParams(effects.query()).get("action"), "scale_in");
  const filters = { action: "scale_out", resource_type: "k8s_workload", status: "failed", q: "a&b / 中文", page: 3, page_size: 20 };
  const exportQuery = new URLSearchParams(effects.query(filters, false));
  for (const key of ["action", "resource_type", "status", "q"]) assert.equal(exportQuery.get(key), filters[key]);
  assert.equal(exportQuery.has("page"), false);
  assert.equal(exportQuery.has("page_size"), false);
  assert.equal(new URLSearchParams(effects.query(filters)).get("page"), "3");
});

test("reporting period uses local epoch times identically in list and CSV", () => {
  const filters = { from_local: "2026-09-01T09:15", to_local: "2026-09-09T18:30", page: 2, page_size: 20 };
  for (const paginated of [true, false]) {
    const params = new URLSearchParams(effects.query(filters, paginated));
    assert.equal(params.get("from_ms"), String(new Date(filters.from_local).getTime()));
    assert.equal(params.get("to_ms"), String(new Date(filters.to_local).getTime()));
    assert.equal(params.has("from_local"), false);
  }
  const blank = new URLSearchParams(effects.query({ from_local: "", to_local: "" }));
  assert.equal(blank.has("from_ms"), false);
  assert.equal(blank.has("to_ms"), false);
  const openEnd = new URLSearchParams(effects.query({ from_local: filters.from_local }));
  assert.equal(openEnd.has("from_ms"), true);
  assert.equal(openEnd.has("to_ms"), false);
  for (const from_local of ["not-a-date", "2026-02-30T10:00", "2026-09-01T25:00"]) assert.throws(() => effects.query({ from_local }), /有效/);
  assert.throws(() => effects.query({ from_local: filters.to_local, to_local: filters.from_local }), /晚于/);
  assert.throws(() => effects.query({ from_local: filters.to_local, to_local: filters.to_local }), /晚于/);
});

test("invalid submitted period displays an error without requesting or changing export scope", async () => {
  const nodes = Object.fromEntries(["form", "#effects-print", "#effects-detail", "#effects-result", "#effects-csv", "#effects-filter-error"].map(key => [key, { innerHTML: "", handlers: {}, addEventListener(type, callback) { this.handlers[type] = callback; } }]));
  const host = { innerHTML: "", querySelector: key => nodes[key], addEventListener() {} };
  const requests = [];
  const win = { addEventListener() {}, ResourceApi: { async requestJson(url) { requests.push(url); return { items: [], summary: {}, total: 0, page: 1 }; } } };
  vm.runInNewContext(fs.readFileSync("static/js/scaling-effects.js", "utf8"), { window: win, URLSearchParams, document: { getElementById: () => host }, FormData: class { constructor(values) { this.values = values; } get(key) { return this.values[key]; } } });
  await win.ScalingEffects.load();
  const priorCsv = nodes["#effects-csv"].href;
  nodes.form.handlers.submit({ preventDefault() {}, currentTarget: { from_local: "2026-09-09T10:00", to_local: "2026-09-01T10:00" } });
  assert.equal(requests.length, 1);
  assert.equal(nodes["#effects-filter-error"].hidden, false);
  assert.match(nodes["#effects-filter-error"].innerHTML, /必须晚于/);
  assert.equal(nodes["#effects-csv"].href, priorCsv);
  nodes.form.handlers.submit({ preventDefault() {}, currentTarget: { from_local: "not-a-date" } });
  assert.equal(requests.length, 1);
  assert.match(nodes["#effects-filter-error"].innerHTML, /有效/);
  nodes.form.handlers.submit({ preventDefault() {}, currentTarget: { from_local: "2026-09-01T10:00", to_local: "2026-09-09T10:00", action: "scale_in" } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.equal(nodes["#effects-filter-error"].hidden, true);
  const list = new URL(requests[1], "https://local.test").searchParams;
  const csv = new URL(nodes["#effects-csv"].href, "https://local.test").searchParams;
  for (const key of ["from_ms", "to_ms"]) assert.equal(list.get(key), csv.get(key));
  assert.match(nodes["#effects-result"].innerHTML, /调配发起时间/);
});

test("API text and evidence identifiers cannot inject markup or URL paths", () => {
  const html = effects.renderMetric({ container: '<img src=x onerror="alert(1)">', metric: "cpu", basis: "request", status: "<script>" }, 0);
  assert.doesNotMatch(html, /<img|<script>/);
  assert.match(html, /&lt;img/);
  assert.equal(effects.detailUrl('a/b?x="'), "/api/scaling-effects/a%2Fb%3Fx%3D%22");
});

test("empty summaries do not claim simulated gains and group units are separate", () => {
  assert.match(effects.renderSummary({}), /尚无可汇总指标/);
  const html = effects.renderSummary({ metrics: [{ resource_type: "openstack_vm", metric: "cpu", basis: "capacity", reclaimed_capacity: -2 }, { resource_type: "k8s_workload", metric: "memory", basis: "request", reclaimed_capacity: 0 }] });
  assert.match(html, /-2 cores/);
  assert.match(html, /0 GiB/);
  assert.match(html, /VM/);
  assert.match(html, /Request/);
});

test("load failure is explicit and stale responses cannot overwrite newer results", async () => {
  const nodes = Object.fromEntries(["form", "#effects-print", "#effects-detail", "#effects-result", "#effects-csv"].map(key => [key, { innerHTML: "", addEventListener() {} }]));
  const host = { innerHTML: "", querySelector: key => nodes[key], addEventListener() {} };
  let finish;
  let calls = 0;
  const win = { addEventListener() {}, ResourceApi: { requestJson() { calls++; return calls === 1 ? new Promise(resolve => { finish = resolve; }) : Promise.reject(new Error('<img src=x> unavailable')); } } };
  vm.runInNewContext(fs.readFileSync("static/js/scaling-effects.js", "utf8"), { window: win, URLSearchParams, document: { getElementById: () => host } });
  const first = win.ScalingEffects.load();
  await win.ScalingEffects.load();
  assert.match(nodes["#effects-result"].innerHTML, /账本加载失败/);
  assert.doesNotMatch(nodes["#effects-result"].innerHTML, /<img/);
  finish({ items: [], summary: {}, total: 0, page: 1 });
  await first;
  assert.match(nodes["#effects-result"].innerHTML, /账本加载失败/);
  assert.equal(new URL(nodes["#effects-csv"].href, "https://local.test").searchParams.get("action"), "scale_in");
});
