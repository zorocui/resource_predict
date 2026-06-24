# 大规模资源详情加载优化设计

## 背景

当前详情请求首次执行时会读取并解析整个 `raw_data.json`，将所有资源的全部指标转换为 pandas 序列，再提取目标资源。应用启动后 VM 与 K8S 调度器还会立即执行拉取和预测，导致首次详情请求与后台任务争抢 CPU、磁盘和内存。资源量和历史点数增加后，任意详情弹窗的冷加载时间随全量数据规模增长。

本次升级将原始数据改为资源级不可变分片，建立 O(1) 内存索引，并将详情元数据与图表数据分开加载。旧产物格式不兼容、不迁移、不回退；升级部署时必须重新生成产物。

## 目标

- 详情请求只读取目标资源的原始指标文件，不解析其他资源。
- 资源定位由线性扫描改为 O(1) 索引。
- 弹窗立即展示列表已有信息，图表按指标、容器和时间窗口异步加载。
- 应用启动后 API 先可用，后台首次更新延迟执行。
- 增量更新只重写发生变化的资源分片。
- 保持现有预测、调配建议、K8S 容器粒度规格、图表模式、手动拉取、自动拉取、部分预测和真实调配门控功能。
- 产物提交失败时，读取端继续看到上一份完整快照。

## 非目标

- 不兼容或迁移旧 `raw_data.json`。
- 不保留旧格式读取回退、兼容 shim 或双写。
- 不改变预测模型、决策阈值或调配执行语义。
- 不通过降低预测数据精度换取性能；裁剪仅作用于图表响应。

## 产物结构

VM 与 K8S 分别在 `outputs/vm/`、`outputs/k8s/` 使用相同结构：

```text
outputs/<scope>/
├─ raw_index.json
├─ raw/
│  ├─ 2f/
│  │  └─ <resource-hash>-<content-hash>.json
│  └─ a8/
│     └─ <resource-hash>-<content-hash>.json
├─ summary_index.json
├─ details/
│  └─ part-00000.json
├─ manifest.json
├─ forecast_error_report.json
└─ generation_stats.json
```

`resource-hash` 为 `SHA-256(resource_id)`，前两位作为目录名。`content-hash` 基于序列化后的资源原始数据计算。文件名不包含资源 ID，避免非法字符、路径长度和碰撞问题。

单个 raw 文件只包含一个资源：

```json
{
  "resource_id": "k8s:cluster-a:payments:deployment:api",
  "resource_type": "k8s_workload",
  "spec": {"containers": {}},
  "metrics": {
    "cpu_limit": {"timestamps": [], "values": []},
    "cpu_request": {"timestamps": [], "values": []},
    "memory_limit": {"timestamps": [], "values": []},
    "memory_request": {"timestamps": [], "values": []}
  },
  "container_metrics": {}
}
```

`raw_index.json` 只保存全局元数据和资源引用：

```json
{
  "schema_version": 2,
  "generated_at_epoch_ms": 1781767200000,
  "freq": "5min",
  "resources": {
    "k8s:cluster-a:payments:deployment:api": {
      "file": "raw/2f/<resource-hash>-<content-hash>.json",
      "resource_type": "k8s_workload",
      "points": 2016,
      "updated_at_epoch_ms": 1781767200000
    }
  }
}
```

## 原子提交与清理

全量生成和增量更新均采用不可变资源文件、索引最后提交：

1. 在内存中生成新索引。
2. 仅为内容发生变化的资源写入新的不可变 raw 文件。
3. 每个 raw 文件使用临时文件加 `os.replace` 原子落盘。
4. 所有资源文件成功后，原子替换 `raw_index.json`。
5. 索引提交成功后，将不再引用的 raw 文件留在 300 秒安全宽限期；后续任一提交清理已过宽限期的孤立文件和空目录。

如果步骤 2 至 4 失败，旧索引仍指向旧完整文件。读取端不会看到半份快照。宽限期允许并发请求完成已经取得旧索引引用的读取；清理失败只记录告警，不影响新索引使用。

## 读取架构

新增 `RawResourceStore`，职责仅包括：

- 按 mtime 缓存 `raw_index.json`。
- 建立 `resource_id -> raw file ref` 字典。
- 根据资源 ID 读取单个 raw 文件并转换其指标。
- 使用按条目数限制的 LRU 缓存保存热点资源。
- 提供全量迭代能力给预测管线，不为详情请求预载全量数据。

`_SingleForecastStore` 在 summary 载入时同步建立 `resource_id -> summary item` 字典。详情查询流程为：

```text
summary_by_id[resource_id]
→ detail_ref 定位预测详情分片
→ RawResourceStore.get(resource_id)
→ 只合并当前资源
```

删除 `_get_raw_by_id()` 及其全量 pandas 缓存。预测管线需要全量数据时通过 raw 索引逐个读取；部分预测只读取指定资源。

## API 设计

### 现有详情接口

保留 `GET /api/resources/<resource_id>` 的完整功能语义，但底层只读取目标资源。既有 API 调用方不会丢失字段。

增加查询参数：

```text
include_charts=true|false
history_points=<positive integer>
```

默认 `include_charts=true` 保持 API 功能；项目自带前端使用 `include_charts=false` 快速加载元数据。

### 图表接口

新增：

```http
GET /api/resources/<resource_id>/charts
    ?metric=cpu_limit
    &container=api
    &history_points=1000
```

规则：

- `metric` 必须属于当前资源类型允许的指标。
- `container` 仅用于 K8S 容器图表。
- 默认返回最近 1000 个训练历史点，并完整保留测试窗口和未来预测。
- `history_points` 最大 10000；超限返回 400。
- 未传 `metric` 时返回当前资源全部 Workload 级指标，但仍执行窗口限制。
- 完整历史通过明确的时间范围参数分段请求，不提供无边界超大响应。

响应包含图表块和实际窗口元数据，便于前端说明当前展示范围。

## 前端加载流程

列表行已经包含规格、建议、数据质量和紧急度。点击详情后：

1. 立即打开弹窗并渲染列表已有信息。
2. 请求 `include_charts=false` 补充详情元数据。
3. 异步请求当前默认指标的图表。
4. 切换指标时才加载对应指标。
5. 切换容器时才加载对应容器指标。
6. 使用 `resource_id + metric + container + history_points` 作为图表缓存键。
7. 同一请求进行中时复用 Promise，避免重复请求。
8. 请求失败时保留元数据显示局部错误和重试按钮，不关闭整个弹窗。

前端缓存使用 LRU 上限，避免长时间浏览大量资源造成内存持续增长。

## 图表窗口与数据精度

预测和决策始终使用完整原始序列。图表接口只在响应构造阶段裁剪训练历史：

- 默认历史点数：1000。
- 最大单次历史点数：10000。
- 测试集和未来预测不裁剪。
- 时间范围请求在 pandas 序列转换为 JSON 数组之前完成过滤。

因此优化不会影响模型拟合、误差报告、建议动作或执行门控。

## 预测详情分片

将 `GenerationConfig.detail_chunk_size` 默认值从 200 调整为 25。summary 中继续保存准确的 `detail_ref.file` 与 `detail_ref.offset`，读取端只解析包含目标资源的小分片。

这项调整不改变详情内容，只减少冷加载 JSON 解析和缓存占用。

## 启动更新延迟

为 VM 和 K8S 后台调度分别增加启动延迟配置，默认 60 秒：

- `UpdateConfig.startup_delay_seconds`
- `K8SPrometheusConfig.scheduled_update_startup_delay_seconds`

调度线程启动后先使用可中断等待，再执行首次自动更新。页面、列表和已有详情产物可立即访问。手动拉取不受延迟影响。应用停止时等待可被 stop event 立即打断。

## 旧产物处理

- 删除 `RAW_DATA_FILENAME = "raw_data.json"` 及所有单体 raw 读写路径。
- 删除 manifest 作为旧详情回退来源的读取逻辑；summary/detail/raw 索引缺失时明确报产物不完整。
- 健康检查将 `raw_index.json` 和其引用文件作为必需产物。
- 检测到 `raw_data.json` 不进行读取、迁移或回退。
- 部署文档要求停止应用、删除 `outputs/vm` 与 `outputs/k8s`，再执行全量生成。

## 配置

新增或调整默认值：

```text
GenerationConfig.detail_chunk_size = 25
GenerationConfig.detail_history_points_default = 1000
GenerationConfig.detail_history_points_max = 10000
GenerationConfig.raw_resource_cache_items = 100
UpdateConfig.startup_delay_seconds = 60
K8SPrometheusConfig.scheduled_update_startup_delay_seconds = 60
```

## 可观测性

详情请求日志记录：

- resource ID
- summary 索引命中
- raw 资源缓存命中
- raw 文件读取耗时
- 预测详情分片读取耗时
- 合并与裁剪耗时
- JSON 响应点数和估算字节数
- 请求总耗时

生成统计增加 raw 文件数、复用文件数、新写文件数、清理文件数和索引字节数。

## 功能回归矩阵

| 范围 | 必须验证的行为 |
| --- | --- |
| VM 全量生成 | 生成 raw 索引和资源文件；列表、详情、图表、误差报告可用 |
| K8S 全量生成 | Workload 与容器指标完整；`spec.containers` 保持容器粒度 |
| VM 增量更新 | 只更新目标资源文件；其他资源引用不变 |
| K8S 增量拉取 | 新资源可插入，已有资源按时间戳去重合并，未更新资源不重写 |
| 部分预测 | 只读取和重算指定资源；其他预测详情保留 |
| 详情接口 | 完整详情字段保持；后端不读取其他 raw 文件 |
| 图表接口 | 指标、容器、窗口过滤正确；测试集和未来预测完整 |
| 前端弹窗 | 元数据立即显示；图表异步；切换指标/容器和重试正常 |
| 建议与调配 | action gate、置信度、数据质量、冷却期、策略层级门控不变 |
| 状态账本 | `action_gate_state.json` 连续轮次累计不受 raw 分片影响 |
| 并发读写 | 更新期间详情读取旧或新完整快照，不出现半文件 |
| 启动调度 | 自动更新延迟，手动更新立即执行，停止可中断等待 |
| 产物健康 | 缺索引、缺资源文件、错误哈希和未知资源类型均能报告 |
| 旧产物 | 仅有 `raw_data.json` 时明确失败，不回退、不迁移 |

## 性能验收

- 应用启动后 2 秒内列表 API 可用，不被首次自动更新占用。
- 详情元数据接口不触发全量 raw 读取。
- 冷缓存详情只打开一个 raw 文件和一个预测详情分片。
- 默认图表响应每个指标最多包含 1000 个训练历史点。
- 同一资源第二次访问命中 LRU，不重复解析文件。
- 详情请求内存增量与单资源数据量相关，不与资源总数线性增长。
- 在代表性内网数据上记录详情元数据和默认图表接口的 P50/P95；目标分别不高于 200ms 和 500ms。

## 验证命令

实现后执行项目完整回归：

```bash
python -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
python -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
python -m pytest -q
```

另增加大数据合成基准，证明单资源详情读取不会调用全量数据集读取，也不会打开非目标资源文件。
