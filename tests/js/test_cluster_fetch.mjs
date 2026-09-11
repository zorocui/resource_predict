import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync("static/js/index.js", "utf8");
const fetchBody = source.slice(source.indexOf("  async function fetchK8sPrometheusData("), source.indexOf("  function addVmClusterRow("));
const bindBody = source.slice(source.indexOf("  function bindClusterConfigEvents("), source.indexOf("  function bindFilters("));

test("cluster row fetch submits only that cluster with the requested window", async () => {
  const requests = [];
  let click;
  const context = {
    app: { els: { k8sClusterList: { addEventListener: (_, handler) => { click = handler; } } } },
    api: { postJson: async (url, body) => { requests.push({ url, ...JSON.parse(JSON.stringify(body)) }); return {}; } },
    rowValue: row => row.cluster,
    setClusterConfigMessage() {}, setView() {}, startUpdatePolling() {},
    collectClusterConfigs: () => { throw new Error("Single-cluster fetch must not collect other clusters"); },
  };
  vm.runInNewContext(`${fetchBody}\n${bindBody}\nbindClusterConfigEvents();`, context);
  for (const [cluster, full] of [["cluster-c", true], ["cluster-a", false], ["", true]]) {
    const attribute = full ? "data-k8s-fetch-full" : "data-k8s-fetch-single";
    const button = { closest: () => ({ cluster }), hasAttribute: name => name === attribute };
    click({ target: { closest: selector => selector.includes(`[${attribute}]`) ? button : null } });
    await Promise.resolve();
  }
  assert.deepEqual(requests, [
    { url: "/api/cluster-configs/k8s-fetch", clusters: ["cluster-c"], full_refresh: true },
    { url: "/api/cluster-configs/k8s-fetch", clusters: ["cluster-a"], full_refresh: false },
  ]);
  assert.match(source, /data-k8s-fetch-full[^>]*>全量拉取<\/button>/);
});
