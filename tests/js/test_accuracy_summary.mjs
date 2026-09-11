import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
const window = {};
vm.runInNewContext(fs.readFileSync("static/js/accuracy-summary.js", "utf8"), { window });
const data = { accuracy: null, rows: [], runs: [], resource_count: 0, valid_points: 0, hit_points: 0, invalid_points: 0, absolute_unit_points: 0 };
test("empty accuracy is not zero and requires no manual evaluation", () => {
  const html = window.ForecastAccuracy.render(data);
  assert.match(html, /—/);
  assert.match(html, /下一次预测完成后自动显示/);
  assert.doesNotMatch(html, /预测兑现评估|独立历史测试<\/span>|name="source"/);
});
test("CSV exports every row, safely, with the scoring rule", () => {
  const rows = Array.from({length: 101}, () => ({resource_id: "=formula", accuracy: 0.5}));
  const csv = window.ForecastAccuracy.csv({...data, rows});
  assert.equal(csv.split("'=formula").length-1, 101);
  assert.match(csv, /5个百分点/);
  assert.match(csv, /实际值绝对值×5%/);
});

test("new rule and pending old summaries are clearly identified", () => {
  const pending = { ...data, needs_regeneration: ["k8s"] };
  const html = window.ForecastAccuracy.render(pending);
  assert.match(html, /4000%/);
  assert.match(html, /200 个百分点/);
  assert.match(html, /重新预测/);
  assert.match(html, /旧口径，未计入当前准确率/);
  assert.match(window.ForecastAccuracy.csv(pending), /待重新预测范围（旧口径未计入）/);
});
