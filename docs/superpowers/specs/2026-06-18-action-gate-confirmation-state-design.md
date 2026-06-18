# 调配建议连续轮次确认状态设计

## 背景

当前 K8S 与 VM 决策结果会输出 `action_gate.required_consistent_rounds`，但每次预测都会重新生成 `observed_consistent_rounds`，没有读取上一轮状态。只要所需轮次大于 1，建议就会长期停留在 `observe`，无法通过后续一致预测自动进入 `ready`。

本设计为调配建议增加跨预测轮次、跨进程重启的连续确认状态。连续性按“同一资源、同一动作方向”判断，目标规格允许随预测结果变化。

## 目标

- 成功完成一轮预测后，按资源累计连续一致的扩容或缩容建议。
- 达到策略要求的确认轮次后，将 `action_gate.state` 设置为 `ready`。
- 保持现有执行前门控：置信度、数据质量、冷却期、策略层级及 K8S 目标策略仍须独立通过。
- 支持服务重启、全量预测和部分资源预测。
- 在前端明确展示已确认轮次和所需轮次。

## 非目标

- 不要求连续轮次的目标规格完全一致。
- 不改变扩容、缩容动作的判定算法和默认确认轮次。
- 不允许轮次达标绕过其他执行门控。
- 不把决策状态写入 `raw_data.json` 或 Workload 当前规格。

## 方案

### 独立确认状态账本

每个资源类型在自身输出目录保存 `action_gate_state.json`：

- K8S：`outputs/k8s/action_gate_state.json`
- VM：`outputs/vm/action_gate_state.json`

文件按 `resource_id` 保存最新动作方向、连续轮次、最近成功确认时间和状态版本。状态使用现有原子 JSON 写入能力落盘，避免进程中断产生半写文件。

示例：

```json
{
  "schema_version": 1,
  "resources": {
    "k8s:cluster-a:payments:deployment:api": {
      "action_direction": "scale_in",
      "consistent_rounds": 2,
      "last_confirmed_at": "2026-06-18T08:00:00Z"
    }
  }
}
```

状态协调放在预测结果汇总阶段执行，不在并行 worker 中直接读写账本。这样一次预测任务对每个资源最多计数一次，也避免多个 worker 竞争写文件。

## 状态转换规则

一次“有效轮次”指该资源完成预测计算，并随整批预测产物成功写出。仅触发 Prometheus 拉取、拉取失败、预测失败或写出失败均不算一轮。

动作归一化为以下方向：

- `scale_out`、`scale_out_candidate` → `scale_out`
- `scale_in`、`scale_in_candidate` → `scale_in`
- `hold`、`mixed`、`insufficient_data` 或未知动作 → 无调配方向

转换规则：

1. 本轮方向与账本方向相同：`consistent_rounds + 1`。
2. 本轮为扩容或缩容，但方向与账本不同：方向更新，轮次设为 `1`。
3. 本轮无调配方向：删除该资源的连续确认状态，输出轮次为 `0`。
4. 资源未参与本轮预测：保持原状态，不增加轮次。
5. 账本缺失、损坏或版本不支持：记录告警并从空状态恢复，不阻断预测。

`observed_consistent_rounds` 最大显示为 `required_consistent_rounds`，内部账本可同时将计数封顶，避免无意义增长。满足以下条件时轮次门控进入 `ready`：

```text
action_direction 存在
且 observed_consistent_rounds >= required_consistent_rounds
```

否则为 `observe`。`hold` 沿用当前无需执行的语义，不将其视为可执行调配建议。

## 数据流

1. worker 根据预测结果生成基础 `scaling_advice` 和 `required_consistent_rounds`。
2. 汇总阶段读取对应资源类型的确认状态账本。
3. 状态协调器依据本轮动作更新每个已预测资源的连续轮次。
4. 协调器回填 `observed_consistent_rounds`、`action_gate.state` 和可读原因。
5. 预测产物成功写出后，原子写入更新后的账本。

为避免“账本已前进、预测产物未写出”的不一致，应先完成预测产物写出，再提交账本；如果账本提交失败，本轮结果仍可展示，但记录明确告警，下一轮从旧账本继续，最多损失一轮计数，不产生错误放行。

## 部分预测与资源生命周期

- 部分预测只更新本轮实际重算的资源；保留其他资源的账本状态。
- 全量预测中暂时缺失的资源不立即清零，以容忍短暂采集缺失。
- 状态记录超过保留期限后清理。建议默认保留 30 天，并做成配置项。
- 资源重新出现时，如果旧状态尚未过期且方向一致，可以继续累计；过期后从第 1 轮开始。

## 执行安全

轮次门控只负责确认建议方向的稳定性。创建真实 `execute` 任务前，继续强制检查：

- `action_gate.state=ready`
- `confidence=high` 且分数达到阈值
- `data_quality` 达标
- 不在冷却期，或使用现有明确授权的冷却期覆盖路径
- `policy_tier` 有效
- K8S `target_k8s_policy.ready_for_execution=true`

人工复核和手动目标规格路径保持现有语义，不因本次设计扩大权限。

## 前端展示

前端根据 `action_gate` 展示明确进度：

- 未达标：`建议方向已连续确认 2/3 轮，需继续复核或人工确认后调配`
- 已达标：`建议方向已连续确认 3/3 轮，轮次门控已通过`

如果其他执行门控未通过，按钮状态和提示应继续反映实际阻塞原因，不能仅依据轮次达标显示为可执行。

## 测试策略

单元测试覆盖：

- 同方向连续累计并在阈值轮次进入 `ready`。
- 扩容与缩容互相切换时从 `1` 重新开始。
- `hold`、`mixed`、`insufficient_data` 清除连续状态。
- 目标规格变化不重置同方向计数。
- 计数封顶、账本缺失、损坏和版本不支持。
- 保留期限清理。

集成测试覆盖：

- 连续三次成功缩容预测形成 `1/3 → 2/3 → 3/3 ready`。
- 拉取成功但预测失败不增加轮次。
- 预测产物写出失败不提交账本。
- 服务重启后继续累计。
- 部分预测不改变未参与资源的状态。
- 并行 worker 不造成重复计数或状态文件竞争。
- 达到轮次后，其他执行门控仍能阻止不安全任务入队。

## 文档更新

实现时同步更新：

- `docs/architecture.md`：移除“未持久化跨预测轮次计数”的限制说明，补充状态账本和转换规则。
- `docs/api-reference.md`：说明 `observed_consistent_rounds` 的累计语义。
- `docs/configuration.md`：如引入状态保留期限配置，增加对应配置项。
