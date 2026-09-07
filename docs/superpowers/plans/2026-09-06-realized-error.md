# 真实误差回填实施计划

目标：实现已授权的历史预测自动评分，在当前 forecast-evaluation-baseline 分支继续。

1. 新增 pipeline/realized_error.py：SQLite 唯一批次/曲线/目标点，流式导入已有留档；用显式 observation_evidence 匹配待评点，事务提交和聚合 JSON 报告，模块命令可重试。
2. providers/k8s_prometheus.py 在填补前生成同频真实采样证据；pipeline/prepare.py、data/io.py、data/updater.py 保留证据和规格快照，data/raw_store.py 提交后触发评分。pipeline/run.py 留档后导入新预测并将状态写入 generation_stats。
3. tests/test_realized_error.py 验证首次评分不变、迟到补评、容器与规格隔离、数据证据和时间约束、过期清理、失败重试；provider 与 raw/updater 测试验证实际链路。
4. 更新 docs/configuration.md、architecture.md、development.md，说明 schema、SQL 明细、重试命令和证据限制。
5. 使用 .venv/Scripts/python.exe 运行 compileall、pyflakes、vulture --min-confidence 80、pytest -q，检查 diff 并清理 .venv 外 __pycache__。

约束：不新增依赖，UTF-8 编辑，文档命令面向 Linux；不改执行门控；以万级资源和容器指标为规模基线。无需 UI 和新服务。

## 完成记录（2026-09-06）

五项工作已完成。新增自动评分账本、采样证据传递、重试命令、报告与文档；增加 12 项回归测试。评分使用批次事务避免逐资源磁盘提交；汇总仍遍历保留期内账本，尚未实测生产规模吞吐和存储占用。

最终验证：compileall、pyflakes、vulture --min-confidence 80 均通过；pytest 425 passed、28 subtests passed。保留一个基线已有的 NumPy 非有限输入警告。变更保留在当前分支工作区，未推送。
