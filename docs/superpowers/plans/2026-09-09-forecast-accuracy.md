# 预测准确性与佐证实施计划

用户已同意按容差达标率+真实误差+完整性统计量化预测准确性，并增加页面、导出和冻结快照。沿用 writing-plans/subagent-driven-development 流程，保留既有评分、调配成效及routing工作。

## 数据与统计合同

- 独立历史测试与预测兑现评估分开，默认source=realized；legacy只展示旧误差记录与证据不足提示，不回填逐点或宣称独立测试。
- 百分比利用率序列以百分点展示MAE/RMSE/P95，±5/±10个百分点为规则容差达标率；绝对量保留cores/GiB误差，这两档达标率为null。不是置信度、100%-MAPE或目标上界覆盖率。
- 兑现使用已有forecast_realized.sqlite3，冻结预测与真实值同口径匹配。按资源/容器/指标/单位/目标时刻/提前量桶选最新留档版本，先去重再按模型筛选，不能看实际误差挑预测。提前量以target-issued_ms计算；issued_ms是留档批次/生成时间代理，不等于前端正式可见时间。
- 独立历史测试新增归档逐点证据，保留所有模型在同一外层测试段的predicted/actual、训练截止、测试时间、selection和provenance。测试来自预处理历史序列，不等于生产实测；只接受evaluation.role=independent_test，旧产物不追认。
- 汇总按resource_type、level(resource/container)、metric、model、unit、horizon分组，显示count、resource_count、mae、rmse、p95_error、hit_rate_5pp、hit_rate_10pp、macro_hit_rate_5pp、macro_hit_rate_10pp、underestimate_rate。分母只使用有效配对点；coverage单独显示全部候选/去重/到期/有效配对及各排除项。
- ±5/10pp含边界；零负载有效，非有限与缺失不可作零。P95采用nearest rank。Resource与Container分开，CPU/内存与Request/Limit不混成单一准确率。
- 查询接口GET /api/forecast-accuracy：source(realized/holdout/legacy)、resource_type、level、metric、model、horizon(0-1h/1-6h/6-24h/>24h)、q、from_ms/to_ms（目标时刻含开始不含结束）、page/page_size。返回version/source/generated_at_ms/policy/coverage/summary/items/total/page/page_size/warnings。
- items逐点字段resource_id/resource_type/container/metric/model/unit/horizon/batch/issued_ms/target_ms/data_end_ms/predicted/actual/error/abs_error/status/hit_5pp/hit_10pp；summary上述字段，coverage里candidate_points/selected_points/duplicate_points/due_points/matched_points/observation_coverage以及状态计数。曲线由筛选后的逐点明细展示，不用未来重算曲线覆盖历史。
- CSV同一筛选范围支持summary/points。POST /api/forecast-accuracy/snapshots 冻结筛选、汇总及全部逐点证据（流式gzip JSONL），保存SHA256清单；返回snapshot_id和下载URL。快照不随默认7天留档清理而消失。GET快照下载只返回已完成原子发布的包，路径严格限制。
- 页面顶部新增“预测准确性”：两个新评估入口+旧报告参考、独立筛选、容差说明、完整率和误差汇总、预测/实际对照、明细分页、CSV、快照保存/下载、打印。无真实账本时为真实空态。

## 执行步骤

- [x] 1. 新预测保存独立测试逐点证据，导入现有留档SQLite，旧记录保持缺失。
- [x] 2. 实现SQL只读评估会话、去重、分组统计和分页/流式逐点导出；不在每页反序列化全量历史。
- [x] 3. 实现API、旧报告参考、CSV和完整快照；实现页面与截图/打印。
- [x] 4. 同步README文档索引与详细文档；针对边界、取数完整性、重复/模型过滤、快照可复算与保留期测试；全量compileall+pyflakes+vulture80+pytest及Node、浏览器验证。

## 完成记录

全量653 passed、28 subtests passed，保留既有NumPy非有限输入警告；compileall、pyflakes、vulture --min-confidence 80（含benchmarks）通过。前端35项测试、JS语法和git diff --check通过。

独立复核修正了冻结测试缺失标签被误标为“等待观测”的问题；invalid_prediction单独计数，skip_reason随证据导出。快照同时保存原单位候选和展示单位选中点，可核验去重及模型筛选；同一读事务与原子ZIP发布复核通过。

实际应用兑现空态通过；旧报告入口读取324条误差行、45个资源，明确不是逐点证据。通过页面保存旧参考快照 `010afa6dc98f4cff89e33c6920cc7d5b`，point_count=0、record_count=324。对照散点图和打印用仅浏览器响应替身验证，资源名明确为非生产样本，未写入真实预测账本。

既有评分、调配成效和routing修改均保留，未提交或推送。测试缓存沿用此前自动审批删除限制，未换用其他方式绕过。
