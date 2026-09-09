# 成本感知路由：新时间段确认协议

> 方法审计补充：见 [routing-method-audit.md](routing-method-audit.md)。现有程序跨预测时刻分配预算，
> 成本为阶段合成估计。本协议暂不能用于判定在线预算验证通过；继续积累数据，待逐批约束和成本核验完成后
> 再发布新版确认协议。无需现在重复运行或回传文件。以下原始登记内容保留，不回写历史判据。

协议版本：routing-confirmation-v1。登记日期：2026-09-08。
本协议在已有 2352 条开发记录分析完成后制定，仅约束尚未查看的新时间段。
不是对旧结果的事前注册，也不构成生产验收或统计概率保证。

## 1. 固定研究对象

- 继续使用内网已运行成功的 calibration 综合包，不修改模型、特征、种子、校准分位数或选型规则。
- 主要候选：conservative_q90 + predicted_gain_per_cost，主要预算档 0.5。
- 主要对照：同一 conservative_q90 成本估计下的 predicted_gain、random_positive_gain；random 为辅助对照。
- 五种成本和四档预算继续全部输出，其他配置作为探索性结果，不用测试结果替换主要候选。
- 所有预算仍是训练数据确定的名义追加墙钟预算，不是硬实时或总 CPU 预算。
- 学习程序冻结，模型参数按既定训练/校准划分重新拟合；并非把旧批次拟合权重固定部署。

## 2. 内网留存与新数据

在内网登记旧开发批次最大 test_end、固定包 MANIFEST.json、Python/依赖版本、硬件和实验参数。
原始明细与精确日期不外传。旧开发数据作为训练历史可以重用，新一轮用于测试的窗口必须晚于旧开发最大 test_end，
且不能是已查看过的新一轮测试结果。记录资源增删变化，不因某个资源效果差而将其移除。

优先保持相同 30 个 Workload、10 分钟采样、4 小时预测和 seed=42。
本工具 limit=30 是随机抽样上限，资源清单变化会改变抽样；需要在内网核对实际清单，
若无法保持相同资源，汇报“资源组成变化”，不可视作纯时间变化实验。

建议采集一个完整的 **14 天新测试区间**，覆盖工作日与周末，并在其前保留至少约 14 天历史。
约 28～30 天固定快照可支持这个设置；不要求先改系统默认 30 天 raw 保留配置。
这里的 14 天是整个回放的测试范围；综合分析的三个时间留出只评价后半部分，
前半部分用于学习，不能把整个 14 天都称为外层测试。

停止同时更新实验副本。拷贝 raw_index.json 及全部所引用 raw 分片，检查副本可读且索引保持一致。
历史缺失、资源下线或样本不足按现有规则报告，不补造数据、不放宽时间隔离。

## 3. 一次回放、一次汇总

进入内网已有 calibration 包目录，使用现有 Python 环境。
以下 horizon=24 **仅适用于 10 分钟采样**，84 个不重叠起点对应 14 天：

```bash
python -X utf8 -m benchmarks.routing_share --check-raw /data/frozen/k8s-new --limit 30 --seed 42 --origins 84 --horizon 24 --output preflight-confirmation.json
python -X utf8 -m benchmarks.routing_pilot --raw-dir /data/frozen/k8s-new --limit 30 --seed 42 --origins 84 --horizon 24 --output pilot-k8s-confirmation
python -X utf8 -m benchmarks.routing_calibration --run-dir pilot-k8s-confirmation --output share-routing-confirmation.json
```

请先检查预检中的 eligible、horizon_hours、history_days 和 model_fits_planned。
拟合次数约为上一轮 14 起点的 6 倍（序列数不变时），耗时不保证线性。
将训练和测试放在同一无额外高负载的机器上串行执行，并保留内网日志。
没有新时间段时，先留存旧包和快照，等待新观测；重复回放旧数据不会增加时间泛化证据。
不要为了凑 84 起点覆盖回旧开发测试范围。

## 4. 预先固定的判断规则

以下是本轮研究筛选标准，由本协议预先固定；5% 是研究容忍线，不是已获准的生产超支额度。
优先检查三个时间留出中的主要候选（Q90、收益/成本、0.5 档）：

1. 三个区间均有效，无快照变化；不足则为证据不足，不算通过或失败。
2. 每个区间实际追加成本不超过名义预算的 105%；同时报告严格零超支次数。
3. 三个区间平均预算利用率不低于 70%，防止主要通过闲置预算降低超支。
4. 每个区间总归一化收益非负；至少两个区间严格高于正预测收益候选内随机选择的平均收益。
5. 与仅收益排序逐区间比较收益和实际成本：若收益更高但实际花费也更高，只记为取舍，
   不宣称成本排序独立优于收益排序。除去不超过 1e-9 的浮点差异后再比较。

十次资源留出作为辅助检查，逐次呈现，不把划分当作十批独立数据。
四档预算均保留，不根据本轮结果改主要预算档。以上全部满足只能支持下一步影子运行可行性研究，
不能证明统计显著、业务 SLA 改善、费用节省或严格预算保证；没有授权执行真实调配。
不满足时记录失败方向，未来改方法后使用另一批未见数据，不能调参后复用本批作为最终验证。

## 5. 减少文件往返

本轮不需要现在发文件。待新批完整运行后，只发审核通过的 `share-routing-confirmation.json`，并附一句：
“10 分钟/4 小时，84 起点；新测试晚于旧开发区间；资源组成是否变化；机器环境是否变化。”
无需发送原始数据、资源名、日期、preflight、pairs、report 或日志。
若多集群/多批已经具备条件，可用 --run-dir 一次传入多个结果目录，仍生成一个汇总；说明批次顺序即可。

## 6. 版本指纹

当前本地源码 SHA-256 如下（文件字节包括换行符）。以实际内网已使用包的 MANIFEST.json 为完整版本依据；
本地原 ZIP 已不在 dist，无法核对其压缩包摘要，不能宣称重新核验了原 ZIP。
如内网 manifest 与下表不一致，请内部核对原因，不要覆盖旧包；换行差异也可能导致哈希变化。

| 文件 | SHA-256 |
| --- | --- |
| benchmarks/routing_pilot.py | 2DBB50E83D0D627E40280C12BA366ADC58A3C12CAEE485AB8DBD1A7A5A047878 |
| benchmarks/routing_share.py | 4B3F721B665739DB88E5721ED6001783C60AB55BFF0E183449B6FA9CEE7085F5 |
| benchmarks/routing_validation.py | 6CB8C18B42D2D47D69D7A33223E225362BB9A5EF6097916669B9945AA9B0D69E |
| benchmarks/routing_budget.py | 6CAEA1EB633E71F15CC45AA05C98A46B7024A05E2C4B529361194776A9C45CF1 |
| benchmarks/routing_calibration.py | 0A4DE1D0DCA050BF354F6B4D0C8E431AA2C1F1027012771F24371F1777905B5C |
| tools/build_routing_package.py | 71DBFC71C2AFB240D0C6F1BD00ABEF8B5FE52B5CD958BA7D27A2E2C9ABC41886 |
