# API 接口文档与使用示例

本文档详细说明系统所有 API 端点及完整使用方法。

## 页面路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Web 首页（SPA） |

## 资源查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/resources` | 资源列表（支持分页、筛选、搜索） |
| GET | `/api/resources/<id>` | 资源元数据详情；可选返回 charts |
| GET | `/api/resources/<id>/charts` | 按指标、容器和时间范围加载目标资源图表 |
| GET | `/api/resources/<id>/feedback` | 当前资源的启用评审、规则、报告更新时间与受控配置状态；只读，不触发评分 |
| GET | `/api/resources/details?ids=a,b` | 批量详情（最多 100 个） |
| GET | `/api/resources/advice-summary` | 建议统计（action/confidence 计数） |
| GET | `/api/resources/<id>/scaling-history` | 资源调配历史 |

### 列表参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `q` | string | 搜索 resource_id / IP / namespace / workload / node |
| `action` | string | 筛选动作：`scale_out` / `scale_in` / `hold` / `mixed` / `scale_out_candidate` / `scale_in_candidate` / `insufficient_data` |
| `resource_type` | string | 筛选类型：`openstack_vm` / `k8s_workload` |
| `sort_by` | string | 排序：`urgency_score`（默认）/ `resource_id` / `anomaly_score` |
| `page` | int | 页码（从 1 开始） |
| `page_size` | int | 每页数量（默认 20，最大 200） |
| `top_n` | int | 返回前 N 条（优先于分页） |

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
- `/api/upsert-data` 新增资源时，该资源必须提供所有指标的完整非空序列
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

## 预测上界附加字段

页面资源详情新增“校准与验证”，显示正式采用/观察/待重新核验状态、上界完整性、影子分配量、评审有效期及未通过项。图例“校准上界”可开关预测上界曲线；null 时段保留断开。旧预测或缺少真实报告时展示等待状态，不生成演示评分。

`GET /api/resources/<id>/feedback` 返回 resource_id、server_time_ms、report_status（available/missing/stale/error）、report_generated_at_ms、assessment（仅当前资源，可能为 null）、rules、policy_enabled、resource_allowlisted。24 小时以上或时间异常的报告标记 stale，资源不存在返回 404。页面展示的是最近发布报告，不代替执行前的实时核验。

受控启用能力默认关闭。配置开关及显式资源列表开启后，满足当前批次判定的正式建议可带 `scaling_advice.calibration_activation.status=active`；其 baseline_advice 为回退快照，valid_until_epoch_ms 为授权证据期限。`prediction_upper_bound.mode=active` 且 applied_to_targets=true 表示正式采用，其余仍为观察。此能力没有新增启用 API，具体配置和失败回退规则见 configuration.md。

启用评审判定位于各 scope 的 `forecast_realized_report.json.activation_assessment`，本轮未新增自动启用 API。`resources[].status=eligible_for_review` 只表示满足报告中的经验评审条件；消费者必须校验 valid_until_epoch_ms、当前规格/配置及最新数据，不能将它作为 execute 授权。

资源摘要与详情还可返回 `shadow_comparison`：version、mode=shadow、executable=false、status、reason、baseline、candidate、source_spec、forecast_windows、budgets。只有完整新预测才生成 paired；部分校准或局部重算返回 unavailable。baseline/candidate 是确认前的 action/target_spec/policy_tier 快照，既有 scaling_advice 仍是正式建议。实际配对评分在各 scope 的 `forecast_realized_report.json.shadow_comparison`，本轮未增加执行或报表 API。

资源详情图表及容器详情图表增加可选 `calibration`：`status`、`mode=observe`、`target_coverage=0.95`、与 `x_pred_ms` 对齐的 `upper`、`buckets` 样本统计及口径。`upper=null` 表示该时段样本不足；partial 不代表全窗口有效。

建议对象增加 `prediction_upper_bound`：默认 `applied_to_targets=false`、`metrics[]`（container、metric、status、upper_peak、complete、unit）。默认观察模式不修改既有 action、confidence、规格目标或扩缩容授权；显式受控采用时按上文标记 active。旧产物可以没有该字段。完整算法及覆盖率报告见 [configuration.md](configuration.md)。
