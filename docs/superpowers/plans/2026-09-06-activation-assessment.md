# 校准启用判定实施计划

1. 新增 pipeline/activation_assessment.py：连续可比轮次、指标身份与完整性、同目标去重及配对统计；按资源输出固定规则版本的评审判定。
2. realized_error.py 在报告事务内追加 activation_assessment；不新增执行入口，不改门控，不在 raw 提交重复运行。
3. 新增 tests/test_activation_assessment.py，使用小型真实 SQLite 账本验证可用/不可用边界、模型/口径重置、去重先于评分筛选、K8S 容器及报告入口。
4. 更新 configuration、architecture、development 和 API 文档，记录阈值、报告时效、只供评审及当前局限。
5. 使用本地 .venv Python 执行 compileall、pyflakes、vulture --min-confidence 80、pytest -q；检查 diff、清理缓存，保留已有未提交工作。

## 完成记录（2026-09-07）

以上五项已完成。判定按资源输出连续可比轮次、完整指标证据、阈值与失败原因、有效截止时间；自动启用固定为 false，正式策略和 execute 门控不消费判定。

本轮新增 22 项测试，全量 476 passed、28 subtests passed；compileall、pyflakes、vulture --min-confidence 80（含 benchmarks）和 git diff --check 通过。保留基线已有的 NumPy 非有限输入警告。

合成配对基准：1000 个 K8S 资源、每资源 2 容器、每容器 4 指标、16 轮，每曲线 12 点，共 1536000 点；判定耗时 5.56 秒，数据库约 160 MB。数据人为构造为满足规则，仅验证计算及开销，不代表生产收益。结果保存在 outputs/activation_benchmark.json；合成数据库随临时目录自动清理。

所有改动保留在当前分支工作区，未提交、未推送；尚未在本任务中部署或启用正式策略。
