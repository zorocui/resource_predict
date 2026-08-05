# K8S 自动拉取单点序列频率兜底设计

## 问题

K8S Prometheus 自动拉取会在合并数据后立即触发局部预测。新发现的 Workload 如果首批有效指标只有一个时间点，`infer_sample_interval_seconds` 无法根据时间戳差值推断采样间隔。预测配置使用 `workload_test_duration=24h` 和 `workload_future_duration=24h` 时，窗口换算因缺少频率而抛出：

```text
无法按时长 '24h' 换算点数：时间序列频率未知
```

当前更新器还会在无法推断时把 K8S raw 元信息错误回退为通用的 `h`，导致即使绕过异常也可能把 5 分钟数据的 24 小时窗口错误换算为 24 点。

## 目标

- 自动拉取使用页面数据采集配置中的 `step_seconds` 作为权威频率提示；
- 正常拥有至少两个不同时间点的序列仍优先使用实际时间戳间隔；
- 单点或无法推断的序列使用 raw 元信息频率换算时长窗口；
- 单点新资源因历史点数不足时进入既有“跳过并等待后续数据”流程，不使整个自动更新任务失败；
- VM、手工 push 和已有规则序列行为保持不变。

## 数据流

`run_k8s_prometheus_upsert` 从当前不可变运行配置快照取得 `step_seconds`，转换为 pandas 可识别的固定频率字符串，例如 `300s`，并作为 `freq_hint` 传给 `run_upsert_with_data`。

更新器在 K8S 路径收到 `freq_hint` 时：

1. 首次创建 raw 存储时使用该提示，而不是通用 `h`；
2. 已有 raw 存储时仍以本次显式提示为准，使页面修改后的采样步长能在下一次更新生效；
3. 写回前不使用无法推断的单点序列覆盖提示频率；只有没有显式提示时才维持现有自动推断行为。

`generate_forecasts` 在 predict-only 模式已从 raw 元信息读取 `freq`。它把该值作为 `fallback_freq` 传给 `resolve_forecast_window`。窗口解析器先计算实际序列间隔；仅当结果为空时，才把 `fallback_freq` 转换为固定秒数。两者都不可用时保留现有明确异常。

## 接口变化

- `run_upsert_with_data(..., freq_hint: Optional[str] = None)`：新增可选频率提示；现有调用无需修改。
- `_do_update(..., freq_hint: Optional[str] = None)`：内部传递提示。
- `resolve_forecast_window(..., fallback_freq: Optional[str] = None)`：只在实际间隔未知时使用。

频率转换只接受可转换为固定 `Timedelta` 的 pandas 频率。无效或非固定提示视为不可用，不覆盖实际推断，也不隐藏最终的“频率未知”错误。

## 错误与短序列处理

频率提示解决的是窗口点数换算，不伪造历史数据。单点资源换算出 288 点窗口后，预测流水线会根据既有 `min_len <= test_size` 规则跳过该资源，记录警告并保留 raw 数据。后续自动拉取积累足够数据后，该资源自然进入预测。

一个集群中存在单点新资源时，不再因为窗口解析异常导致其他已具备足够历史的资源全部更新失败。

## 测试

- 单点 K8S 序列配合 `fallback_freq="300s"` 能把 24 小时换算为 288 点；
- 无 fallback 的单点时仍抛出频率未知错误；
- 实际 15 分钟序列与 `300s` fallback 同时存在时仍采用实际 15 分钟间隔；
- K8S Prometheus 更新调用把运行配置的 `step_seconds` 转为频率提示；
- 单点 upsert 写入的 raw 元信息保持配置频率；
- 现有完整频率推断、预测窗口和自动调度测试全部通过。

