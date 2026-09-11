# API 接口文档与使用示例

本文档详细说明系统所有 API 端点及完整使用方法。

当前预测准确率页面使用 `GET /api/forecast-accuracy/summary` 读取轻量JSON。返回实际选用模型的独立历史测试准确率（绝对误差不超过 `max(5个百分点, 实际值绝对值×5%)` 的达标点/有效点）、资源数、样本数、测试区间和各scope更新时间。汇总产物版本为2；版本1的旧口径结果不计入，待重新预测的scope列在 `needs_regeneration`。无有效样本为null，读取损坏文件为503。旧逐点只读接口不再用于默认页面，逐点留档与SQLite导入已删除。

## 页面路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Web 首页（SPA） |

## 资源查询

预测准确性使用独立接口：`GET /api/forecast-accuracy`、`GET /api/forecast-accuracy/export.csv?kind=summary|points`、`POST /api/forecast-accuracy/snapshots`、`GET /api/forecast-accuracy/snapshots/<id>/download`。支持来源、资源/容器层、指标、模型、提前量和目标时刻筛选；容差达标率与完整率返回0..1，百分点误差已经乘100。快照保存全部筛选证据，默认7天账本清理不影响已保存ZIP。字段、统计和佐证边界见 [forecast-accuracy.md](forecast-accuracy.md)。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/resources` | 资源列表（支持分页、筛选、搜索） |
| GET | `/api/resources/<id>` | 资源元数据详情；可选返回 charts |
| GET | `/api/resources/<id>/charts` | 按指标、容器和时间范围加载目标资源图表 |
| GET | `/api/resources/details?ids=a,b` | 批量详情（最多 100 个） |
| GET | `/api/resources/advice-summary` | 建议统计（action/confidence 计数） |
| GET | `/api/resources/<id>/scaling-history` | 资源调配历史 |

### 列表参数

调配成效使用独立只读接口：`GET /api/scaling-effects`（列表及汇总）、`GET /api/scaling-effects/<task_id>`（单事件）、`GET /api/scaling-effects/export.csv`（筛选范围完整CSV）、`GET /api/scaling-effects/<task_id>/evidence.json`（原始证据包及SHA256）。列表/CSV支持 `resource_type`、`action=scale_in|scale_out|mixed|unknown`、`status`、`q`（资源或任务ID子串）、`from_ms`（调配开始时间含边界）、`to_ms`（不含边界）；列表另支持 `page`、`page_size=1..200`。详情返回 `{schema_version,event,sha256}`；列表返回 `{version,policy,summary,items,total,page,page_size,generated_at_ms}`。未知任务404，非法参数400，账本读取失败503。没有账本时返回真实空列表，不创建示例数据。默认政策版本2（`evaluation_mode=next_collection`）在调配后的下一次有效采集确认生效后，即以调配前最近采样和本轮调配后采样形成 `evaluated` 结果并进入汇总；不等待稳定期或24小时窗口。单点前后指标的 `observation_kind=snapshot`、`start_ms=end_ms` 为采样时间；`coverage`、`valid_hours`、P95、超限时长及 `reclaimed_unit_hours` 为null。CSV另包含 `evaluation_mode`、`before_observation_kind`、`after_observation_kind`。`provisional` 保留用于历史窗口事件，旧默认窗口未完成事件在下次有效采集升级，已评估历史结果不改写。完整字段、算法和证据契约见 [scaling-effects.md](scaling-effects.md)。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `q` | string | 搜索 resource_id / IP / namespace / workload / node |
| `action` | string | 筛选动作：`scale_out` / `scale_in` / `hold` / `mixed` / `scale_out_candidate` / `scale_in_candidate` / `insufficient_data` |
| `resource_type` | string | 筛选类型：`openstack_vm` / `k8s_workload` |
| `confidence` | string | 按实际分数筛选 `high` / `medium` / `low`；缺失或非法分数为 `unknown` |
| `sort_by` | string | 排序：`urgency_score`（默认）/ `resource_id` / `anomaly_score` |
| `page` | int | 页码（从 1 开始） |
| `page_size` | int | 每页数量（默认 20，最大 200） |
| `top_n` | int | 返回前 N 条（优先于分页） |

### 评分字段（v2）

列表、单资源及批量详情中的 `urgency_score` 统一为 `0..100`。`urgency_breakdown` 包含 `version=2`、`score_max=100`、`score`、`level`、`kind`、`components[{label,value}]`、`metric_scores[{metric,container,action,value}]`。`kind` 为 `capacity_risk`（容量风险）、`savings`（节省机会）、`none` 或 `unknown`；`level` 为 `low`、`medium`、`high`、`critical`、`none` 或 `unknown`。默认分级边界为40、70、90；无有效证据的排序值0必须结合 `unknown` 显示为待评估，不能当作低风险。等级未经生产回放校准。

新预测产物的 `scaling_advice.confidence_breakdown` 包含 `version=2`、`score_max=100`、`score`、实际 `components[{label,value}]`。现有 `confidence_score` 仍为百分制，低<45、中45–<72、高≥72；`confidence_metric_scores` 保存资源级扣分前的单项分。K8S 增加 `container_advice`，保存容器的动作、单项分、统计、缺失基线和阻断指标；统计中的 `sample_count` 是有效预测点数。详情和摘要预测产物均保留这些字段。

置信度是规则证据分，不是正确概率；混合方向扣8并封顶71，不能达到高置信度建议执行门槛。缺旧版分解时不要前端反推公式，也不要把缺失分数当0。历史产物不自动重写，重新预测后获得新版置信度；紧急度在读取 API 时按新规则计算。完整公式与限制见 [architecture.md](architecture.md#置信度评分v2)。

建议汇总 `confidence_counts` 同时包含 `unknown` 计数。筛选、统计和页面等级均以实际分数为准，不用旧标签覆盖分数。

### 详情接口特殊状态

当资源正在等待预测完成时，详情接口返回 HTTP 202 并包含 `prediction_pending: true` 标记。批量详情接口同样处理。

详情元数据与图表拆分加载：

| 接口 | 参数 | 说明 |
| --- | --- | --- |
| `/api/resources/<id>` | `include_charts` | 默认 `true`；前端弹窗首屏传 `false`，只读取摘要和预测详情分片 |
| `/api/resources/<id>` | `history_points` | 图表历史点数，默认 1000，最大 10000 |
| `/api/resources/<id>/charts` | `metric` | 必填，只返回目标指标 |
| `/api/resources/<id>/charts` | `container` | 可选，只返回目标容器的该指标 |
| `/api/resources/<id>/charts` | `history_points` | 可选兼容参数；传入时限制历史点数，最大 10000；不传时返回时间范围内全部训练历史 |
| `/api/resources/<id>/charts` | `start_ms` / `end_ms` | 可选毫秒时间范围；前端图表按钮优先使用时间范围过滤 |

弹窗应先请求 `include_charts=false` 并立即展示规格、建议和门控状态，再按可见指标异步请求 `/charts`。每个图表请求只会读取该资源对应的一个 raw 分片，不扫描其他资源。

图表块除 `x_train_ms`、`x_test_ms`、`x_pred_ms` 及对应值外，还包含以下时间与缺口元数据：

| 字段 | 说明 |
| --- | --- |
| `test_end_ms` | 最后一个有效测试数据点的毫秒时间戳 |
| `sample_interval_seconds` | 本轮预测采用的规范采样间隔；K8S Workload 使用运行配置中的 `step_seconds` |
| `max_interpolation_gap_steps` | 可自动补齐的最大连续缺失步数，同时用于前端判断是否断线 |

后端保证 `x_pred_ms` 的首点为 `test_end_ms + sample_interval_seconds`，其余未来点保持相同间隔。前端会再次过滤所有不晚于 `test_end_ms` 的未来点；黄色预测区域从 `test_end_ms` 开始，到最后一个有效未来预测点结束。若没有严格晚于测试终点的未来点，则不显示黄色区域。历史和测试曲线遇到超过允许步数的大缺口会断开。

K8S 指标的 `data_quality` 会附带 `recent_contiguous_points`、`recent_contiguous_span_hours`、`data_end_ms` 和 `prediction_skipped`。当最近连续段的点数不足测试窗口时，该指标记为跳过；若 Workload 因指标过短无法重算，接口继续提供其已有预测产物。可在 `manifest.json` 与 `forecast_error_report.json` 的 `meta.prediction_skips`、以及 `generation_stats.json` 顶层的 `prediction_skips` 中查看 `resource_id`、`metric` 与原因 `recent_contiguous_segment_too_short`。

## 数据更新

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/update-status` | 查询更新任务状态 |
| GET | `/api/update-history?limit=20` | 查询最近更新历史，`limit` 为 1–100 |
| POST | `/api/update-trigger` | 触发 pull 型增量更新（同步） |
| POST | `/api/update-data` | 推送增量数据，仅更新已有资源（异步） |
| POST | `/api/upsert-data` | 推送数据，更新或新增资源（异步） |

更新任务成功、部分成功或失败后都会写入 `outputs/update_history.json`，应用重启后仍可查询。系统按完成时间从新到旧保留最近 100 条；历史文件读取或写入异常只记录日志，不影响数据更新主流程。历史记录包含任务来源、拉取窗口、开始/结束时间、耗时、资源和数据点统计以及错误信息。整体 `status` 可为 `success`、`partial_success` 或 `failed`；K8S Prometheus 更新还通过 `cluster_results` 记录每个集群的 `success` / `failed`、Workload 数、耗时和错误。

历史总耗时优先按有效的 `finished_at-started_at` 计算，包含采集、合并和预测；旧记录即使误存了仅处理阶段的 `elapsed_seconds`，读取时也会校正，无需重新拉取或重写历史文件。缺少时间边界时才回退到原耗时。K8S更新结果的 `elapsed_seconds` 为整轮采集和处理耗时，另保留 `fetch_elapsed_seconds` 与 `upsert_elapsed_seconds`；逐集群耗时仍仅表示对应集群拉取耗时。

K8S 多集群拉取中，只要至少一个集群成功且后续 upsert/预测完成，同时另有集群失败，整体即为 `partial_success`。失败集群的具体异常写入对应 `cluster_results[].error`；成功集群的数据继续提交。失败集群所属或本轮未返回的 Workload 会保留已有 raw 历史和预测产物，不会因为一次稀疏结果被删除。一个 range 查询的任一分片在重试耗尽后失败时，该集群查询按整体失败处理，不会合并部分时间范围。

集群查询成功但聚合不出任何 Workload 时，会先按 15 秒、30 秒退避整轮重试，最多 3 次尝试；仍失败才写入 `cluster_results[].error`。该字段会写明具体断点（容器使用率序列为空 / `kube_pod_owner` 无结果或查询异常 / owner 标签不匹配 / CPU 与内存序列无法配对）并附 `（已连续尝试 N 次）` 后缀，便于区分偶发的监控侧缺数据和持续的配置问题。网络与 HTTP 错误不在这层重试范围内，仍由请求级 `request_max_attempts` 处理。

```json
{
  "records": [
    {
      "status": "partial_success",
      "task_source": "页面手动拉取",
      "fetch_window_label": "增量窗口：最近 7 小时",
      "cluster_results": [
        {
          "cluster": "cluster-a",
          "status": "success",
          "resources_fetched": 32,
          "elapsed_seconds": 12.4,
          "error": null
        },
        {
          "cluster": "cluster-b",
          "status": "failed",
          "resources_fetched": 0,
          "elapsed_seconds": 3.1,
          "error": "Prometheus timeout"
        }
      ]
    }
  ]
}
```

### 更新触发（同步）

`POST /api/update-trigger` 调用 `IncrementalProvider` 拉取增量数据并重新预测。如果已有更新任务在执行，返回 HTTP 409。

### 推送数据格式

```json
[
  {
    "resource_id": "vm-prod-001",
    "resource_type": "openstack_vm",
    "spec": {"cluster": "cluster-openstack-a", "cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
    "metrics": {
      "cpu":    {"timestamps": [1778500000000, ...], "values": [0.62, ...]},
      "memory": {"timestamps": [...], "values": [...]},
      "disk":   {"timestamps": [...], "values": [...]}
    }
  }
]
```

- `timestamps`：毫秒级 Unix 时间戳（也支持秒级和 ISO 字符串）
- `values`：使用率小数 `[0, 1]`
- K8S Workload 可额外携带 `container_metrics.<container>.<metric>`；系统会继续保留 Workload 级 `metrics` 作为汇总视图，并对 container 级序列分别预测。资源详情会返回 `container_charts.<container>.<metric>`，前端在同一 ECharts 图中展示多个 container 的实际/预测曲线。
- 多 container Workload 的 request/limit 建议写入 `scaling_advice.target_spec.containers.<container>`；副本数建议仍写入 Workload 级 `scaling_advice.target_spec.replicas`。
- `/api/update-data` 和 `/api/upsert-data` 均为异步接口（HTTP 202），合并与预测在后台线程执行
- `/api/upsert-data` 新增资源时，该资源必须提供所有指标的完整非空序列。单个新增资源校验失败（如指标缺失、序列为空或时间和值数量不一致）时跳过该资源，其余资源继续合并和预测；可在 `/api/update-status` 的 `last_result.warnings` 中查看资源 ID 和跳过原因。若整批没有任何资源被更新或新增，任务仍返回失败。
- 并发冲突时返回 HTTP 409，查询 `/api/update-status` 确认当前状态

## 调配

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/resources/<id>/scale` | 创建调配任务 |
| GET | `/api/scaling-tasks/<id>` | 查询调配任务 |
| POST | `/api/scaling-tasks/<id>/confirm` | 确认 OpenStack resize |

### 创建调配任务

```json
{"mode": "dry_run"}
```

```json
{"mode": "execute", "confirm": true, "operator": "ops"}
```

| 参数 | 说明 |
| --- | --- |
| `mode` | `dry_run`（仅生成计划）或 `execute`（实际执行） |
| `confirm` | `execute` 模式必须为 `true` |
| `operator` | 操作人标识 |
| `target_spec` | 可选，覆盖预测建议的目标规格 |
| `confirm_create_flavor` | 可选，允许自动创建 OpenStack flavor |
| `target_source` | 可选，标记目标规格来源：`suggested`（默认建议）、`confirmed`（人工复核后的建议）、`manual`（手动目标规格） |
| `ignore_cooldown` | 可选，`true` 表示操作人已人工复核风险并跳过本次冷却期门控；默认 `false` |

`execute` 模式会在入队前执行门控校验；`dry_run` 只生成计划，不执行命令，也不要求 `action_gate=ready`。

自动建议执行（`target_source=suggested` 或未传）必须同时满足：

- `action_gate.state=ready`，即建议已达到当前策略层级要求的确认轮次。
- `action_gate.observed_consistent_rounds` 按同一资源、同一扩缩容方向跨成功预测轮次累计；目标规格变化不重置计数，动作反向时从 1 重新开始，保持/混合/数据不足会清零。
- `confidence=high` 且 `confidence_score >= 72`。如果资源历史覆盖不足 5 天且建议不是 `hold`，`scaling_advice.history_warning` 会说明短历史风险，`confidence_score` 会被降级到执行阈值以下。
- `policy_tier` 为 `conservative` / `balanced` / `aggressive` 之一。
- 相关指标的数据质量满足执行要求：K8S Workload 的相关 request/limit 指标必须为 `data_quality=good`；VM 若记录了非 good 的指标质量，也会阻断。
- 当前资源不在冷却期内：扩容默认 60 分钟，缩容默认 360 分钟，可由 `risk_profile.cooldown_minutes` 覆盖。
- K8S Workload 的 `target_k8s_policy.ready_for_execution` 不为 `false`；多容器 Workload 的建议 request/limit 目标必须写入 `target_spec.containers`。

人工复核建议执行（`target_source=confirmed`）用于“混合信号”或 `action_gate=observe` 但操作人已复核目标规格的场景。该模式只跳过 `action_gate.state=ready` 检查，仍然要求高置信度、有效策略层级、数据质量、冷却期和 K8S 目标策略通过。

手动目标规格执行（传入 `target_spec`，或 `target_source=manual`）使用操作人提供的目标规格。该模式不要求建议自身的 `action_gate` 和置信度达标，但仍需通过有效策略层级、数据质量、冷却期和 K8S 目标策略校验。

任一门控失败都会返回 `execution gate blocked scaling: ...` 并拒绝创建执行任务。
如需在开发、纠错或紧急恢复场景下重复调配同一资源，可在确认风险后传入 `ignore_cooldown=true`；该参数只跳过冷却期检查，仍保留数据质量、策略层级、置信度和 K8S 目标策略等其他门控。

### 任务状态流转

```text
queued -> running -> plan_built -> executing_command -> command_finished
  -> updating_snapshot -> completed (success)
  -> waiting_confirm (OpenStack 手动 confirm)
  -> failed
```

## 配置管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/cluster-configs` | 读取集群配置 |
| PUT | `/api/cluster-configs` | 保存集群配置 |
| POST | `/api/cluster-configs/k8s-diagnose` | 诊断 K8S Prometheus 连通性 |
| POST | `/api/cluster-configs/k8s-fetch` | 拉取 K8S Prometheus 数据（异步） |
| GET | `/api/system-config` | 读取页面统一运行配置与集群配置 |
| PUT | `/api/system-config` | 校验、保存并立即应用统一配置 |

### 统一运行配置读写

预测模型开关（`enabled_methods`、`enable_ensemble`）没有独立端点，统一走 `/api/system-config`
的 `runtime.prediction` 段。`GET` 返回 `runtime`、`vm_scaling_clusters`、`k8s_prometheus_clusters`、
`supported_methods`、`warnings` 和 `paths`；`PUT` 请求体：

```json
{
  "runtime": {
    "collection": { ... },
    "prediction": {
      "vm_test_duration": "72h",
      "vm_future_duration": "24h",
      "workload_test_duration": "24h",
      "workload_future_duration": "24h",
      "enabled_methods": ["seasonal_naive", "prophet"],
      "enable_ensemble": false
    },
    "decision": { ... }
  },
  "vm_scaling_clusters": { ... },
  "k8s_prometheus_clusters": [ ... ]
}
```

保存成功后写入 `deploy/runtime_config.json` 并替换内存中的运行配置快照，因此后续采集、预测和
决策任务读到的都是新配置。保存还会唤醒 K8S 后台调度线程重读开关和周期，但唤醒本身不触发拉取：
只有重算出的到期时刻（上一次拉取开始时刻 + 当前周期）已经落在过去时才会立即取数，例如把周期
改短到已逾期，或关闭超过一个周期后重新打开。需要立即取数请显式调用
`POST /api/cluster-configs/k8s-fetch`。校验失败返回 400，运行配置类错误会附带
`field` 字段路径。`runtime`、`vm_scaling_clusters`、`k8s_prometheus_clusters` 三个文件按顺序
写入，任一失败则全部回滚到修改前的内容。

### 集群配置读写

`PUT /api/cluster-configs` 请求体：

```json
{
  "vm_scaling_clusters": { ... },
  "k8s_prometheus_clusters": [ ... ]
}
```

### K8S Prometheus 拉取

`POST /api/cluster-configs/k8s-fetch` 可选传入集群名称列表以仅拉取指定集群：

```json
{"clusters": ["cluster-k8s-a"]}
```

可传入 `full_refresh=true` 强制拉取全量历史窗口：

```json
{"clusters": ["cluster-k8s-a"], "full_refresh": true}
```

该接口为异步（HTTP 202），拉取和预测在后台线程执行。默认情况下，已有本地 K8S raw 基线时只拉取增量窗口：`scheduled_update_interval_minutes + incremental_overlap_minutes`，默认最近 7 小时；本地 raw 数据缺失或 `full_refresh=true` 时拉取 `history_days`，默认最近 7 天。

---

## 使用方法和示例

### VM 数据接入

#### Provider 接入（全量）

Provider 函数返回统一资源结构：

```python
def vm_provider(resources: int, n: int, freq: str) -> list[dict]:
    return [
        {
            "resource_id": "vm-prod-001",
            "resource_type": "openstack_vm",
            "spec": {
                "cluster": "cluster-openstack-a",
                "instance_id": "7b8c1d2e-0000-1111-2222-333344445555",
                "cpu_cores": 4, "memory_gb": 8, "disk_gb": 100
            },
            "metrics": {
                "cpu":    {"timestamps": [...], "values": [...]},
                "memory": {"timestamps": [...], "values": [...]},
                "disk":   {"timestamps": [...], "values": [...]}
            }
        }
    ]
```

#### 增量 pull 接入

配置 `settings.update.incremental_provider_path`，格式为 `module:function`：

```python
def vm_incremental_provider(prepared_resources: list[dict], points_to_add: int) -> list[dict]:
    return [
        {
            "resource_id": "vm-prod-001",
            "metrics": {
                "cpu":    {"timestamps": [1778500600000], "values": [0.69]},
                "memory": {"timestamps": [1778500600000], "values": [0.74]},
                "disk":   {"timestamps": [1778500600000], "values": [0.46]}
            }
        }
    ]
```

手动触发 pull 更新：

```bash
curl -X POST http://127.0.0.1:5000/api/update-trigger
```

#### 推送新增或更新

```bash
# 新增资源（upsert）
curl -X POST http://127.0.0.1:5000/api/upsert-data \
  -H 'Content-Type: application/json' \
  -d '[
    {
      "resource_id": "vm-prod-001",
      "resource_type": "openstack_vm",
      "spec": {
        "cluster": "cluster-openstack-a",
        "instance_id": "7b8c1d2e-0000-1111-2222-333344445555",
        "cpu_cores": 4, "memory_gb": 8, "disk_gb": 100
      },
      "metrics": {
        "cpu":    {"timestamps": [1778500000000, 1778500300000], "values": [0.62, 0.66]},
        "memory": {"timestamps": [1778500000000, 1778500300000], "values": [0.71, 0.73]},
        "disk":   {"timestamps": [1778500000000, 1778500300000], "values": [0.45, 0.45]}
      }
    }
  ]'

# 追加增量数据（update，仅更新已有资源）
curl -X POST http://127.0.0.1:5000/api/update-data \
  -H 'Content-Type: application/json' \
  -d '[
    {
      "resource_id": "vm-prod-001",
      "metrics": {
        "cpu":    {"timestamps": [1778500600000], "values": [0.69]},
        "memory": {"timestamps": [1778500600000], "values": [0.74]},
        "disk":   {"timestamps": [1778500600000], "values": [0.46]}
      }
    }
  ]'

# 查询更新状态
curl http://127.0.0.1:5000/api/update-status
```

### K8S Prometheus 接入

#### 需要的 Prometheus 指标

| 指标 | 用途 |
| --- | --- |
| `container_cpu_usage_seconds_total` | CPU 使用量 |
| `container_memory_working_set_bytes` | 内存使用量 |
| `kube_pod_owner` | Pod -> ReplicaSet/控制器 owner 关系 |
| `kube_replicaset_owner` | ReplicaSet -> Deployment owner 关系 |
| `kube_pod_container_resource_requests*` | CPU/Memory request |
| `kube_pod_container_resource_limits*` | CPU/Memory limit |

Provider 会把 Pod/Container 序列聚合为 `k8s_workload`，同时保留 `container_metrics` 供 container 级预测和图表展示。resource_id 格式为：

```text
k8s:<cluster>:<namespace>:<workload-kind>:<workload-name>
```

#### CLI 使用

```bash
# 临时验证
export K8S_PROMETHEUS_CLUSTERS='{"cluster-k8s-a":"http://127.0.0.1:9090"}'
python ingest_k8s_workloads.py --diagnose

# 正式拉取
python ingest_k8s_workloads.py

# 只拉取指定集群
python ingest_k8s_workloads.py --cluster cluster-k8s-a
```

#### API 触发拉取

```bash
# 拉取全部集群
curl -X POST http://127.0.0.1:5000/api/cluster-configs/k8s-fetch

# 拉取指定集群
curl -X POST http://127.0.0.1:5000/api/cluster-configs/k8s-fetch \
  -H 'Content-Type: application/json' \
  -d '{"clusters": ["cluster-k8s-a"]}'
```

### 调配操作

#### 预检（dry run）

```bash
# VM 预检
curl -X POST http://127.0.0.1:5000/api/resources/vm-prod-001/scale \
  -H 'Content-Type: application/json' \
  -d '{"mode":"dry_run"}'

# K8S Workload 预检
curl -X POST http://127.0.0.1:5000/api/resources/k8s:cluster-k8s-a:prod:deployment:api/scale \
  -H 'Content-Type: application/json' \
  -d '{"mode":"dry_run"}'
```

#### 执行

```bash
curl -X POST http://127.0.0.1:5000/api/resources/vm-prod-001/scale \
  -H 'Content-Type: application/json' \
  -d '{"mode":"execute","confirm":true,"operator":"ops"}'
```

#### 手动确认 resize

如果 `auto_confirm_resize=false`，resize 后任务进入 `waiting_confirm`：

```bash
curl -X POST http://127.0.0.1:5000/api/scaling-tasks/<task_id>/confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirm":true,"operator":"ops"}'
```
