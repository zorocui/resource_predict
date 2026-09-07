const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const host = {innerHTML:'',append(){}};
const esc = x => String(x ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
let payload = {};
const context = {document:{getElementById:()=>host,createElement:()=>({})},window:{
  ResourceList:{escapeHtml:esc,metricTitleFor:(_,m)=>m,typeLabel:()=> 'VM',actionLabel:a=>a},
  ResourcePredictApp:{state:{selectedResourceId:'vm-a'}},
  ResourceApi:{requestJson:async()=>payload}
}};
vm.runInNewContext(fs.readFileSync('static/js/feedback.js','utf8'),context);
(async()=>{
  const resource={resource_id:'vm-a',scaling_advice:{}};
  payload={report_status:'missing',server_time_ms:1000,policy_enabled:false};
  await context.window.ResourceFeedback.load(resource);
  assert.match(host.innerHTML,/尚无真实反馈报告/);
  assert.match(host.innerHTML,/等待新版预测/);
  resource.scaling_advice.prediction_upper_bound={metrics:[{metric:'cpu',upper_peak:null,status:'insufficient_samples'}]};
  payload={report_status:'available',server_time_ms:1000,assessment:{status:'eligible_for_review',valid_until_epoch_ms:2000,paired_runs:16,metrics:[{metric:'<img src=x onerror=alert(1)>',matched_targets:100,due_targets:120,observation_coverage:0.8,baseline_rate:0.1,shadow_rate:0.2,reservation_reduction:null,failed_checks:['observation_coverage']}],reasons:[]}};
  await context.window.ResourceFeedback.load(resource);
  assert.match(host.innerHTML,/具备启用评审条件/);
  assert.match(host.innerHTML,/观测完整率不足/);
  assert.doesNotMatch(host.innerHTML,/<img/);
  assert.match(host.innerHTML,/&lt;img/);
  assert.match(host.innerHTML,/样本不足/);
  payload.server_time_ms=3000;
  resource.scaling_advice.calibration_activation={status:'active',valid_until_epoch_ms:2000};
  await context.window.ResourceFeedback.load(resource);
  assert.match(host.innerHTML,/判定已过期/);
  assert.match(host.innerHTML,/校准建议待重新核验/);
  assert.doesNotMatch(host.innerHTML,/具备启用评审条件/);
  payload.server_time_ms=1000;payload.policy_enabled=true;payload.resource_allowlisted=true;
  await context.window.ResourceFeedback.load(resource);
  assert.match(host.innerHTML,/正式采用校准建议/);
  console.log('Feedback UI states and escaping passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
