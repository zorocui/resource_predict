# K8S 紧急度指标过滤设计

## 背景与问题

K8S Workload 的当前规格只在 `spec.containers.<container>` 中记录 CPU、内存 request/limit，不包含磁盘规格。K8S 建议生成器也只生成 CPU、内存动作，但紧急度服务目前固定遍历 `cpu`、`memory`、`disk`，并在单项动作缺失时回退到资源总体动作。

因此，旧产物、异常产物或混合数据中只要残留 `stats.disk`，磁盘就可能参与 K8S 紧急度计算和 `metric_scores` 展示，造成评分口径与可调配规格不一致。

## 目标

- OpenStack VM 紧急度继续使用 CPU、内存、磁盘指标。
- K8S Workload 紧急度只使用 CPU、内存指标。
- K8S 输入即使残留磁盘统计或磁盘动作，磁盘也不影响总分，且不出现在紧急度分项中。
- 不改变基础动作分、置信度加成、风险分、混合信号、目标变化分和仅分析折扣的现有算法。

## 非目标

- 不为 K8S 容器新增磁盘规格或持久卷容量建议。
- 不批量改写历史预测产物。
- 不调整 VM 的磁盘阈值或磁盘紧急度权重。
- 不改变 K8S 建议生成器的 CPU、内存决策逻辑。

## 方案

在 `resource_predict/services/urgency.py` 的指标贡献计算入口按规范资源类型选择允许指标：

- `openstack_vm`：`cpu`、`memory`、`disk`
- `k8s_workload`：`cpu`、`memory`

过滤发生在读取 `stats` 和 `metric_actions` 之前。这样异常的 K8S `disk` 数据既不会形成原始指标贡献，也不会进入最强指标、其他指标、多指标加成或混合展示链路。资源类型继续通过 `resource_type_of(item)` 归一化，避免在紧急度服务中复制类型别名判断。

目标规格变化仍沿用现有逻辑：VM 比较 CPU、内存、磁盘；K8S 比较可用的 CPU、内存和副本数。由于 K8S 当前规格采用 container 粒度，本次不扩展 container 目标变化的计算范围。

## 数据流与兼容性

1. API 或摘要生成流程把资源项传给 `compute_urgency_breakdown()`。
2. 紧急度服务先解析资源类型，再取得对应的允许指标。
3. 仅允许指标参与单项压力或空闲贡献计算。
4. 总分组件和 `metric_scores` 从过滤后的单项贡献生成。

无需修改输出结构。现有前端继续读取 `urgency_breakdown.metric_scores`，因此会自然停止展示 K8S 磁盘贡献。VM 输出保持兼容。

## 异常处理

- 未识别的资源类型沿用 VM 指标集合，与 `resource_type_of()` 当前默认行为保持一致。
- K8S 的 CPU、内存统计缺失时，继续走现有无指标贡献逻辑。
- K8S 残留磁盘字段不报错，直接忽略，以兼容旧产物和部分更新场景。

## 测试与验收

在 `tests/test_urgency.py` 增加以下回归覆盖：

1. K8S 项同时带 CPU、内存、磁盘动作和统计时，`metric_scores` 只包含 CPU、内存。
2. 对同一 K8S 项增加或删除磁盘残留数据，紧急度总分完全相同。
3. VM 项带磁盘扩缩容信号时，磁盘仍出现在 `metric_scores` 并影响总分。
4. 运行紧急度相关测试；实现属于 Python 逻辑变更，随后按项目约定运行完整回归检查，并清理 `.venv` 外的项目 `__pycache__`。

验收标准是：K8S Workload 的紧急度公式、排序分数和指标贡献中均不存在磁盘贡献，VM 行为不变。
