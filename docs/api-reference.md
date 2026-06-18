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
| GET | `/api/resources/<id>` | 资源详情（含 charts） |
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

## 数据更新

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/update-status` | 查询更新任务状态 |
| POST | `/api/update-trigger` | 触发 pull 型增量更新（同步） |
| POST | `/api/update-data` | 推送增量数据，仅更新已有资源（异步） |
| POST | `/api/upsert-data` | 推送数据，更新或新增资源（异步） |

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
| GET | `/api/forecast-config` | 读取预测模型开关 |
| PUT | `/api/forecast-config` | 保存预测模型开关 |
| POST | `/api/cluster-configs/k8s-diagnose` | 诊断 K8S Prometheus 连通性 |
| POST | `/api/cluster-configs/k8s-fetch` | 拉取 K8S Prometheus 数据（异步） |

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
