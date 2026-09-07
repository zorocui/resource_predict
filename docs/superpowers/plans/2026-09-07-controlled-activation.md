# 受控启用实施计划

1. 从 shadow.py 提取重建完整校准建议的共享函数；新增 controlled_activation.py，检查配置、完整新预测、对应报告与期限，附加回退快照和来源摘要。
2. run.py 在合并后、action_gate 之前应用受控建议；记录采用状态，旧建议可回退。action_gate_state.py 按策略身份隔离确认计数。
3. scaling/tasks.py 对自动建议在 execute 入队及开始运行前复核校准授权时效；不改变现有其他门控和手工覆盖逻辑。
4. 添加配置及真实入队拒绝边界回归，更新 API、配置、架构和开发文档。
5. 本地 .venv 执行 compileall、pyflakes、vulture、pytest；检查 diff 并清理缓存，默认配置保持关闭。

## 完成记录（2026-09-07）

五项已完成：重建完整建议、显式开关和列表、本轮报告/批次核验、回退快照、跨策略确认重置、执行前检查。执行还会读取当前 SQLite 账本，对单个资源重新判定，防止 JSON 延迟发布时旧报告继续授权。

全量 501 passed、28 subtests passed；compileall、pyflakes、vulture --min-confidence 80（含 benchmarks）及 diff --check 通过。保留基线已有 NumPy 非有限输入警告。本轮新增 25 项回归，包括默认关闭、完整采用、失败回退、授权过期/变更、入队/排队后拒绝、报告被新评分推翻及错误字符串列表不能扩大权限。

calibrated_advice_enabled 保持 False，calibrated_advice_resource_ids 保持空元组。没有创建执行任务、开启资源、部署或修改生产规格。所有既有及本轮改动仍保留在当前工作区，未提交、未推送。
