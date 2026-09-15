import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
const window = {};
const elements = Object.fromEntries(["task-history", "task-history-search", "task-history-query", "task-history-prev", "task-history-next", "task-history-total", "task-history-page"].map(id => [id, {}]));
vm.runInNewContext(fs.readFileSync("static/js/scaling-history.js", "utf8"), {window, document:{getElementById:id => elements[id]}});
test("container specs, missing legacy values, and failure targets are truthful and escaped", () => {
  const html = window.ScalingHistory.render([{resource_id:"<script>", mode:"execute", status:"failed", error:"<bad>",
    before_spec:{replicas:3, containers:{app:{cpu_request_cores:0.5, memory_request_gb:1}, sidecar:{cpu_request_cores:0.1}}},
    target_spec:{replicas:2, containers:{app:{cpu_request_cores:0.25}}}}]);
  assert.match(html, /app \/ CPU Request/);
  assert.match(html, /sidecar/);
  assert.match(html, /0.25/);
  assert.match(html, /目标规格（未确认完成）/);
  assert.doesNotMatch(html, /<script>|<bad>/);
  const old = window.ScalingHistory.render([{mode:"execute", status:"success", target_spec:{cpu_cores:4}}]);
  assert.match(old, /未记录/);
  assert.match(old, /调配后规格（命令结果）/);
  const single = window.ScalingHistory.render([{before_spec:{containers:{app:{cpu_request_cores:1}}}, target_spec:{cpu_request_cores:0.5, memory_request_gb:2}}]);
  assert.match(single, /app \/ 内存 Request/);
});
test("loads all resources without a selected resource and paginates", async () => {
  const urls = [];
  const request = async url => { urls.push(url); return {items:[{resource_id:"vm-b"}], page:url.includes("page=2") ? 2 : 1, page_size:20, total:21}; };
  await window.ScalingHistory.load(request);
  assert.match(urls[0], /^\/api\/scaling-history\?/);
  assert.equal(elements["task-history-next"].disabled, false);
  elements["task-history-next"].onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(urls[1], /page=2/);
  assert.equal(elements["task-history-next"].disabled, true);
  elements["task-history-query"].value = "vm-a";
  elements["task-history-search"].onsubmit({preventDefault(){}});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(urls[2], /page=1.*q=vm-a/);
});
