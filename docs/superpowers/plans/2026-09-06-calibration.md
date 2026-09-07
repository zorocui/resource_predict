# 上界校准实施计划

1. 新增 pipeline/calibration.py：按时点读取可比残差，生成经验上界，附加观察建议；只读数据库异常降级，保留基础建议。
2. pipeline/run.py 在新预测留档之前校准，增量合并之后重建注释。forecast_archive.py 保存 calibration；realized_error.py 新增两张表并在真实评分后汇总上界覆盖率。
3. data/io.py 在资源及容器详情 API 保留 calibration；tests/test_calibration.py 覆盖时间隔离、去重、降级、容器、口径、留档、真实评分与增量链路。
4. 更新 configuration、architecture、development、api-reference；说明观察模式、样本要求、数值含义与限制。
5. 使用本地 .venv/Scripts/python.exe 运行 compileall、pyflakes、vulture --min-confidence 80、pytest -q；检查 diff，清理缓存。保留第二轮未提交工作。

## 完成记录

以上五项已完成。新增 16 项测试；全量结果 441 passed、28 subtests passed，保留基线已有 NumPy 非有限输入警告。compileall、pyflakes、vulture --min-confidence 80、git diff --check 均通过。

观察阶段完成：上界已写入预测、建议、留档、详情 API 和覆盖报告；未改变资源目标。生产样本覆盖率、资源节省和万级吞吐未实测。第二轮和本轮改动保留在当前分支工作区，未提交、未推送。
