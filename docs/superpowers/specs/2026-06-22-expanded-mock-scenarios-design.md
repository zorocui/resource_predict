# 扩展模拟资源场景设计

## 目标

`generate_forecasts.py` 默认生成 45 个可复现的模拟资源，其中 18 个 VM、27 个 K8S Workload。模拟数据应稳定覆盖主要资源形态和调配判断，而不是仅增加重复资源数量。

## 场景目录

VM 使用显式、循环的指标场景组合，覆盖整体扩容、整体缩容、保持、CPU/内存/磁盘单项异常、扩缩混合、突发峰值和持续趋势。规格继续覆盖多档 CPU、内存、磁盘和多个集群。

K8S Workload 覆盖：

- Deployment、StatefulSet、DaemonSet、ReplicaSet；
- 单 container、双 container、三 container；
- request + limit、仅 request、仅 limit、无资源基线；
- 扩容、缩容、保持、CPU/内存方向冲突、数据不足；
- 多个 cluster、namespace、节点和副本规模。

场景按资源序号从固定目录选取，随机噪声继续使用稳定种子，保证重复生成结果一致。

## 实现边界

- `GenerationConfig.resources` 默认值改为 45。
- `mock_provider()` 在默认 45 个资源时按 18 VM / 27 Workload 分配；其他数量仍按相同比例合理取整，并确保两类资源至少各一个（总数大于 1 时）。
- 扩展 `resource_predict.providers.mock` 的场景定义，不修改预测输出 schema、决策阈值或执行门控。
- K8S 当前规格继续仅存储在 `spec.containers`，并保持小规格小数粒度。

## 验证

- 单元测试确认 45 个资源的数量和 18/27 分布。
- 断言四种 Workload kind、1/2/3 container、四类资源基线均出现。
- 断言 VM 与 K8S 的负载序列覆盖高、低、中位和混合方向。
- 运行相关 mock provider 测试，并对 Python 文件执行编译和静态检查。
