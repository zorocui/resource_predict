# 建议评分修复实施计划

**Goal:** 修复方向串分和质量扣分丢失，使紧急度、置信度具有可解释的百分制及中文等级。

**Architecture:** 保留 action_gate、confidence、data_quality、cooldown、policy_tier 执行检查。在现有决策模块中统一方向汇总和后端分解，在 urgency 服务计算有界风险/节省机会；前端使用后端分解，不再反推公式。

**Tech Stack:** Python、NumPy、原生 JavaScript；不增加依赖。

## 约束与评分合同

- 保留工作区所有无关修改；本次仅修复评分、展示、测试及对应文档。
- 置信度 v2：扩容只取同方向最强指标，缩容取最弱指标；删除多指标及执行就绪奖励。混合方向扣 8 分并封顶 71；质量与缺基线扣分在容器合并后应用，短历史封顶继续保留。以 `confidence_breakdown` 输出实际加减项、version=2、score、score_max=100。
- 容器合并必须按最终动作选择分数；容器间同指标相反动作仍标记混合。质量扣分同时考虑容器来源；对子建议保存 stats、metric_actions、confidence_metric_scores 供解释与风险评分使用。
- 紧急度 v2：容量风险与节省机会分别标记 kind；取最强相关指标，不再累加 confidence/risk_profile/多指标奖励及执行折扣。扩容触及策略阈值为 40，超阈值压力线性增加至 100（峰值压力折为 0.75）；缩容为 60×保守空闲程度+25×持续低负载比例+15×规格回收比例。容器回收比例按各维度总量和副本变化计算，不保存 Workload 当前汇总规格。
- 紧急度等级：低 <40、中 40–<70、高 70–<90、紧急 >=90；保持为无需调整、无有效证据为待评估。等级是明确标注的规则默认值，未经生产回放校准；不宣称概率或已实现耗尽时间预测。
- 前端合同：`urgency_breakdown` 含 version=2、score_max=100、score、level、kind、components、metric_scores；旧紧急度仅标旧版排序分。置信度显示中文与 `/100`，零分正常显示，缺失不能当零；旧置信度不反推公式。

## 执行步骤

- [x] 1. 修复 VM/K8S 方向汇总、容器质量和混合信号，增加相反动作串分、弱缩容、质量扣分和短历史回归。
- [x] 2. 重写紧急度有界公式，补上容器统计和总容量回收计算，验证有界性、单调性、缺失输入和不受执行权限影响。
- [x] 3. 更新列表与详情中文评分、阈值及实际分解；执行 Node 渲染回归，验证零分和旧产物。
- [x] 4. 同步 docs/architecture.md、docs/configuration.md、docs/api-reference.md；运行本地 .venv 的 compileall、pyflakes、vulture --min-confidence 80、pytest -q，以及 Node 测试和 git diff --check。
- [ ] 测试缓存清理：递归删除与仅针对12个已确认目录中 .pyc 文件的非递归删除均被自动审批拒绝（blocked by policy），未执行删除。代码修复和验证均完成，缓存暂留。

## 完成记录

最终全量回归576 passed、28 subtests passed；保留原有 NumPy 非有限输入警告。compileall、pyflakes、vulture（含 benchmarks）通过；Node 20项通过。使用真实模板与渲染脚本的本地固定数据页面，在1280与768像素宽度下检查了评分排版（不触发真实采集或调配）。独立复核发现并修复了空预测伪零负载、容器缺基线漏扣分；复核通过。API筛选和统计按实际分数统一，缺失或非法分数为 unknown。

四项旧K8S测试曾传入不被生产决策使用的 cpu/memory 键，实际依赖空预测转换为零负载才通过；现改为对应 request/limit 键，保持原有规格和副本行为断言。未改写已有预测产物，未提交或推送，保留原有 README 与 routing 工作。

## 验证命令

```bash
python -m pytest -q tests/test_decision.py tests/test_k8s_workload_decision.py tests/test_urgency.py
node --test tests/js/test_resource_list.mjs tests/js/test_charts.mjs
python -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
python -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
python -m vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
python -m pytest -q
```

本机执行以上 Python 命令使用 `./.venv/Scripts/python.exe`。
