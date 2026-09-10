import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

// Exercise the actual private formatter without bootstrapping the entire page.
const source = fs.readFileSync("static/js/index.js", "utf8");
const body = source.slice(source.indexOf("  function formatDuration("), source.indexOf("  function updateHistoryStatus("));
const format = vm.runInNewContext(`(${body.trim()})`);

test("full update duration displays hours and carries rounded seconds", () => {
  assert.equal(format(4474), "1 小时 14 分 34 秒");
  assert.equal(format(475), "7 分 55 秒");
  assert.equal(format(2096), "34 分 56 秒");
  assert.equal(format(3599.8), "1 小时");
  assert.equal(format(59.8), "1 分钟");
  assert.equal(format(0), "0.0 秒");
  assert.equal(format(null), "-");
});
