# 调配成效实施计划

用户已同意真实调配账本、实测前后对比、页面及导出方案。按 writing-plans 与 subagent-driven-development 流程在当前工作区执行，保留所有既有修改。

## 口径与接口

- 只统计本系统 execute 任务；dry_run 不入账。执行前冻结任务ID、资源、规格、目标和可用原始证据；命令成功仅转 awaiting_effective，后续实测容量符合目标才开始稳定期。
- 默认前24h、稳定1h、后24h，覆盖率至少80%；长缺口不填零、相对变化基线0为null。政策版本1固定留档，报告为观察性对比而非因果证明。
- 以有效采样相邻间隔积分，超过 max_gap_ms 的间隔跳过；CPU核数与内存GiB分别计算。VM为capacity，K8S分别request/limit，容器证据汇总后计算Workload，不能当作物理集群利用率。
- 相同资源下一次execute开始时，前一个后窗口截断并标记 interrupted；失败和外部容量漂移有独立状态，不能静默筛掉负向结果。重复任务ID与重复证据幂等。
- SQLite独立于最近1000条任务文件；仅为已入账资源读取、保存相关窗口证据，按resource/time索引增量计算与分页读取。成功评估冻结，不随当前规格或原始监控滚动删除而改写。未完成证据最多保留前后窗口+7天补齐期。
- `scaling_evidence` schema_version=1，source非mock、collected_at_ms、spec为观测快照，sample_interval_ms；`series`每项为container（VM为空）、metric（cpu/memory）、basis（capacity/request/limit）、unit（cores/GiB）、timestamps（epoch毫秒）、usage、capacity数组，三数组等长，同一时点usage与capacity均为真实监控，不能由当前规格乘历史比例构造。
- API `/api/scaling-effects` GET：resource_type/action/status/q/page/page_size，返回 version、policy、summary、items、total、page、page_size；`/api/scaling-effects/<task_id>` 返回event、metrics及证据曲线；`/api/scaling-effects/export.csv`导出同筛选范围，`/<task_id>/evidence.json`导出完整证据包与SHA256。
- event字段：task_id/resource_id/resource_type/action/status/reason/started_at_ms/completed_at_ms/effective_at_ms/before_spec/target_spec/after_spec/policy/metrics。每metric字段：container/metric/basis/unit/status/before/after/delta_pp/relative_change_pct/reclaimed_capacity/capacity_reduction_pct/reclaimed_unit_hours；before/after包含utilization_pct/mean_usage/mean_capacity/coverage/p95_pct/overload_hours/start_ms/end_ms/valid_hours。summary含event_count/resource_count/status_counts，metrics按resource_type+metric+basis分组，event_count/relative_count/before_pct/after_pct/delta_pp/mean_relative_change_pct/weighted_relative_change_pct/reclaimed_capacity/reclaimed_unit_hours。
- UI新增调配成效页：筛选、汇总、事件表、详情前后曲线与容量、覆盖率、数据来源、CSV、JSON、打印。无数据真实空态，不伪造示例成果；说明按事件统计，资源数去重。

## 工作步骤

- [x] 1. K8S采集真实同期usage/capacity证据并经过raw往返保留；无法取得历史容量时不伪造。
- [x] 2. 实现SQLite成效账本、窗口积分评估、任务与raw提交钩子，覆盖成功/失败/预检/重复/缺失/零基线/再次调配/单位隔离。
- [x] 3. 实现API、前端页面和导出，验证筛选一致、XSS/CSV转义和真实空态。
- [x] 4. 同步文档，代码复核，运行本地.venv的compileall、pyflakes、vulture --min-confidence 80、pytest -q和Node测试，浏览器验证。

## 完成记录

- 全量627 passed、28 subtests passed；保留既有NumPy非有限输入警告。compileall、pyflakes、vulture（含benchmarks）与git diff --check通过；Node28项通过。
- 独立复核发现并修复：窗口结束后误截断、坏证据回滚事件、无界等待、全历史反序列化分页、部分指标过早冻结、批次哈希不可复算、K8S已退出副本导致历史基线被覆盖、容量乘积相等但规格不相等、生效证明被稳定期过滤。
- 浏览器实际应用空态正常；只在浏览器拦截响应进行有结果的曲线/报告/打印验证，样本明确标记非生产实测，未写入真实成效账本、未触发真实调配。实际接口全范围事件数为0。
- 新增报告时间范围from_ms（含）/to_ms（不含），列表、SQL汇总和CSV一致。单个坏资源用savepoint隔离，不回滚其他资源的有效证据。
- 真实VM数据源须提供scaling_evidence；旧任务不伪造历史指标。K8S当前owner映射限制随证据输出；先前有效基线不会被调配后的部分成员历史覆盖。独立复核和新增回归通过。
- 当前工作区保留先前评分修复与无关routing/README工作，未提交或推送。测试缓存清理沿用此前自动审批拒绝的限制，未换用其他删除方式绕过。
