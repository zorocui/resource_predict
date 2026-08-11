# 部署配置与输出结构

本文档详细说明系统的部署配置文件、参数设置和预测产物输出结构。

## 配置文件概览

| 文件 | 用途 | 是否提交 Git |
| --- | --- | --- |
| `resource_predict/settings.py` | 页面启动前必须确定的监听、目录和日志设置 | 是 |
| `deploy/runtime_config.json` | 页面统一管理的数据采集、预测和决策运行配置 | 是 |
| `deploy/clusters.json` | VM / K8S 调配集群配置（含 SSH 凭据） | 否 |
| `deploy/k8s_prometheus_clusters.json` | K8S Prometheus 集群地址与认证 | 否 |
| `deploy/forecast_config.json` | 预测模型开关 | 否 |
| `.env` | 环境变量覆盖 | 否 |

## 集群配置（`deploy/clusters.json`）

从示例文件复制：

```bash
cp deploy/clusters.example.json deploy/clusters.json
```

### OpenStack 集群配置

```json
{
  "cluster-openstack-a": {
    "cloud_type": "openstack",
    "control_host": "192.168.1.10",
    "ssh_user": "root",
    "ssh_port": 22,
    "ssh_key": "/root/.ssh/id_rsa",
    "openstack_rc": "/root/admin-openstack.sh",
    "auto_confirm_resize": false,
    "resize_confirm_poll_interval_seconds": 15,
    "resize_confirm_wait_seconds": 240,
    "command_timeout_seconds": 300,
    "flavor_discovery": "remote",
    "flavor_cache_seconds": 300,
    "auto_flavor_name_prefix": "rp",
    "allowed_flavors": []
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `cloud_type` | 必须为 `openstack` |
| `control_host` | 可执行 `openstack` CLI 的控制节点地址 |
| `ssh_user` / `ssh_port` / `ssh_key` | SSH 登录信息，`ssh_key` 默认 `/root/.ssh/id_rsa` |
| `openstack_rc` | 控制节点上的 OpenStack RC 文件路径，默认 `/root/admin-openstack.sh` |
| `auto_confirm_resize` | 是否自动执行 `resize --confirm` |
| `allowed_flavors` | 可选，限制自动选择的 flavor 名称列表 |

### K8S 集群配置

```json
{
  "cluster-k8s-a": {
    "cloud_type": "k8s",
    "control_host": "192.168.1.20",
    "ssh_user": "root",
    "ssh_port": 22,
    "ssh_key": "/root/.ssh/id_rsa",
    "kubeconfig": "/root/.kube/config",
    "command_timeout_seconds": 300
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `cloud_type` | 必须为 `k8s` |
| `control_host` | 可执行 `kubectl` 的控制节点地址 |
| `kubeconfig` | 控制节点上的 kubeconfig 路径 |

## K8S Prometheus 配置（`deploy/k8s_prometheus_clusters.json`）

```json
[
  {
    "cluster": "cluster-k8s-a",
    "prometheus_url": "http://prometheus.example:9090",
    "namespace_regex": "default|prod",
    "bearer_token": "",
    "basic_auth": "",
    "rate_window": "15m"
  }
]
```

也可通过环境变量临时配置：

```bash
export K8S_PROMETHEUS_CLUSTERS='{"cluster-k8s-a":"http://127.0.0.1:9090"}'
```

## 预测模型配置（`deploy/forecast_config.json`）

```json
{
  "enabled_methods": ["seasonal_naive", "prophet"],
  "enable_ensemble": false,
  "reuse_backtest_model_for_future": true,
  "prophet_routing_enabled": true,
  "prophet_routing_mode": "auto"
}
```

速度优化开关：

| 字段 | 作用 |
| --- | --- |
| `reuse_backtest_model_for_future` | `true` 表示每个模型只在训练窗口拟合一次，并预测 `test_size + future_steps`；前半段用于 holdout 评分，后半段用于未来预测。`false` 保持旧逻辑：用 `y_full` 重新训练未来预测。 |
| `prophet_routing_enabled` | `true` 表示仅在轻量统计特征显示存在明显趋势或季节性时运行 Prophet。若 Prophet 是唯一启用模型，则仍会运行。 |
| `prophet_routing_mode` | `auto` 使用自动路由规则，`always` 表示启用 Prophet 时总是运行，`never` 表示存在其他兜底模型时跳过 Prophet。 |

可在 Web 页面的"预测模型"中启用或关闭模型，保存后写入此文件。

## 全局默认配置（`resource_predict/settings.py`）

`settings.py` 已精简为启动设置，只保留静态/模板/输出目录、日志和 Flask host/port/debug。
业务运行配置请在 Web 的“系统配置”页面修改，保存到 `deploy/runtime_config.json` 后立即对新任务生效。

页面运行配置只保留三组常用字段：数据采集（定时开关、周期、历史、步长、rate 窗口、超时、分片与重试）、
预测（VM/K8S 验证与预测窗口、候选模型、Ensemble）以及决策（策略等级、扩缩容阈值、确认轮次、
冷却时间和命名空间策略）。Prophet 底层参数、缓存、分页、mock 随机种子等实现细节不再作为用户配置。

保存时服务端先校验完整配置；任何字段或集群配置错误都会整体拒绝。调度配置变化会唤醒唯一的
K8S 后台调度线程重新读取开关和周期，不需要重启应用。

### K8S 数据采集可靠性

`deploy/runtime_config.json` 的 `collection` 段包含以下 Prometheus 采集参数：

```json
{
  "collection": {
    "scheduled_update_enabled": true,
    "scheduled_update_interval_minutes": 360,
    "history_days": 7,
    "step_seconds": 600,
    "rate_window": "15m",
    "request_timeout_seconds": 300,
    "range_query_chunk_hours": 24,
    "request_max_attempts": 3,
    "retry_backoff_seconds": 1.0,
    "max_interpolation_gap_steps": 3
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `range_query_chunk_hours` | `24` | `query_range` 单个时间分片的最大时长（小时） |
| `request_max_attempts` | `3` | 每个 Prometheus HTTP 请求的最大尝试次数，包含首次请求 |
| `retry_backoff_seconds` | `1.0` | 首次重试等待秒数；后续按指数退避增长 |
| `max_interpolation_gap_steps` | `3` | 只对不超过该采样步数的完整内部缺口插值；更大的缺口保持断开 |

连接失败、超时、HTTP 429 和 5xx 会在最大尝试次数内重试；其他 4xx 参数或认证错误不会重试。`query_range` 的分片结果按完整 Prometheus 标签集合与时间戳合并，并在边界时间戳重复时保留后一个分片的样本。任一分片在重试耗尽后失败，会使该集群的本轮查询整体失败，不会提交不完整的时间范围。

同一轮中其他成功集群仍会按 `resource_id` 增量 upsert。本轮未返回或失败集群所属的 Workload 不会被当作删除，也不会清空已有 raw 历史和预测产物。采集端仅补齐短缺口；预测端使用最近连续且足够长的数据段，连续段不足测试窗口时跳过该指标或 Workload 的本轮重算并保留旧预测。

旧版 `deploy/forecast_config.json` 仅在 `runtime_config.json` 不存在时作为模型开关迁移来源。

历史默认配置组（仅供理解内部默认值，不再由用户直接编辑）：

| 配置类 | 关键参数 | 默认值 |
| --- | --- | --- |
| `AppConfig` | `host` / `port` / `out_dir` / `log_file` / `debug` | `0.0.0.0` / `5000` / `outputs` / `resource_predict.log` / `False` |
| `GenerationConfig` | `default_test_size` / `default_future_steps` / `freq` / `detail_chunk_size` / `detail_history_points_default` / `detail_history_points_max` / `raw_resource_cache_items` | `72` / `24` / `h` / `25` / `1000` / `10000` / `100` |
| `ForecastConfig` | `enabled_methods` / `enable_ensemble` / `rolling_backtest_folds` / `reuse_backtest_model_for_future` / `prophet_routing_enabled` / `prophet_routing_mode` / `anomaly_route_zscore_threshold` | `("seasonal_naive", "prophet")` / `False` / `1` / `True` / `True` / `auto` / `3.5` |
| `DecisionConfig` | `scale_out_threshold` / `scale_in_threshold` / `scale_in_max_reduction_ratio` / `scale_out_confirmations` / `scale_in_confirmations` / `action_gate_state_retention_days` | `0.8` / `0.2` / `0.5` / `2` / `3` / `30` |
| `UpdateConfig` | `enabled` / `interval_minutes` / `startup_delay_seconds` / `sliding_window` | `False` / `60` / `60` / `False` |
| `K8SPrometheusConfig` | `history_days` / `incremental_overlap_minutes` / `step_seconds` / `rate_window` / `scheduled_update_enabled` / `scheduled_update_interval_minutes` / `range_query_chunk_hours` / `request_max_attempts` / `retry_backoff_seconds` / `max_interpolation_gap_steps` | `7` / `60` / `600` / `15m` / `True` / `360` / `24` / `3` / `1.0` / `3` |

`rate_window` 会用于真实 CPU usage 查询中的 `rate(container_cpu_usage_seconds_total[...])` 窗口；未在集群配置中指定时使用全局默认值 `15m`。默认 `step_seconds=600` 表示每 10 分钟返回一个结果点，两个参数彼此独立。

K8S Prometheus 首次接入、本地 K8S raw 数据缺失或 API 传入 `full_refresh=true` 时，会按 `history_days` 拉取全量历史窗口（默认最近 7 天）。已有本地基线后的普通拉取会使用增量窗口：`scheduled_update_interval_minutes + incremental_overlap_minutes`，默认 `360 + 60 = 420` 分钟，即最近 7 小时。

通过 `python app.py` 启动时，将 `scheduled_update_enabled` 设为 `True` 会启用 K8S Prometheus 后台定时拉取：启动后等待 `scheduled_update_startup_delay_seconds`（默认 60 秒）执行首次拉取，此后按 `scheduled_update_interval_minutes` 执行。VM 数据更新仍需通过页面按钮、更新 API 或 CLI 手动触发。

### 预测窗口配置说明

| 配置 | 作用 |
| --- | --- |
| `default_test_size` / `default_future_steps` | 未设置资源族专用窗口时的兜底点数 |
| `vm_test_duration` / `vm_future_duration` | VM 专用时长，优先于点数 |
| `workload_test_duration` / `workload_future_duration` | K8S Workload 专用时长，默认 `24h` |

VM 时长根据观测到的采样间隔换算点数；K8S Workload 始终以配置的 `step_seconds` 为权威采样间隔，避免 Prometheus 拉取失败形成的大间隔误导窗口换算。例如 `step_seconds=600` + `workload_test_duration="24h"` = 144 个测试点。未来预测时间戳从最后一个有效测试点之后的一个采样间隔开始，不随当前时间或稀疏观测间隔平移。

### 策略分级配置

| 参数 | 说明 |
| --- | --- |
| `default_policy_tier` | 默认策略层级（`balanced`） |
| `conservative_namespaces` | 保守策略命名空间：`prod`, `production`, `payments`, `core`, `platform` |
| `aggressive_namespaces` | 激进策略命名空间：`dev`, `test`, `staging`, `batch` |
| `scale_out_cooldown_minutes` | 扩容冷却时间（默认 60 分钟） |
| `scale_in_cooldown_minutes` | 缩容冷却时间（默认 360 分钟） |

## 输出目录结构

预测产物按资源族物理隔离：

```text
outputs/
├── vm/
│   ├── raw_index.json         # resource_id -> raw 分片的 O(1) 索引
│   ├── raw/                   # 按资源、内容寻址的原始观测分片
│   │   └── ab/<resource-hash>-<content-hash>.json
│   ├── summary_index.json     # 资源列表摘要（含扩缩容建议）
│   ├── manifest.json          # 预测产物清单（不复制历史 charts）
│   ├── forecast_error_report.json # 预测误差报告
│   ├── generation_stats.json  # 本次生成统计
│   └── details/               # 详情分片
│       ├── part-00000.json
│       └── ...
├── k8s/
│   ├── raw_index.json
│   ├── raw/
│   ├── summary_index.json
│   ├── manifest.json
│   ├── forecast_error_report.json
│   ├── generation_stats.json
│   └── details/
│       └── ...
└── scaling_tasks.json         # 调配任务记录
```

## 各文件说明

### `raw_index.json` 与 `raw/`

原始观测数据是预测的唯一输入。每个资源独立保存为不可变、内容寻址的 JSON 文件；`raw_index.json` 只保存资源到分片的引用。完整更新先写新分片，再原子替换索引；部分更新只重写发生变化的资源。

```json
{
  "meta": {
    "schema_version": 2,
    "saved_at_epoch_ms": 1717000000000,
    "resource_count": 1
  },
  "resources": {
    "vm-prod-001": {
      "file": "raw/ab/<resource-hash>-<content-hash>.json",
      "resource_type": "openstack_vm",
      "points": 2016,
      "updated_at_epoch_ms": 1717000000000
    }
  }
}
```

目标分片中保存该资源的 `resource_id`、`resource_type`、`spec`、`metrics` 和可选 `container_metrics`。读取时会同时校验资源 ID、索引路径和内容 SHA-256，详情请求不会读取其他资源分片。

### `summary_index.json`

资源列表摘要，包含扩缩容建议、紧急度、预测方法选择和 anomaly_score。前端列表页直接读取此文件。
每个资源包含轻量 `observed_stats`，按指标保存完整历史观测窗口的 `avg`、`p95`、`peak`。风险队列使用该字段展示资源级统计：VM 为 Resource，K8S 为 Workload 聚合；K8S 详情抽屉在容器图表加载后展示当前选中 Container 的统计，并明确标注范围。`history_coverage` 记录各指标历史覆盖时长，包含 `span_hours`、`span_days`、`threshold_days=5`、`is_short` 等字段；当历史不足 5 天且建议不是 `hold` 时，系统会将建议置信度降级到执行阈值以下，前端也会显示“历史不足 5 天”提示。

### `manifest.json`

预测产物清单和运行元数据，不复制原始历史 charts。资源详情通过 `summary_index.json.detail_ref` 定位小型预测分片，并按需从目标 raw 分片合并图表。

### `details/part-*.json`

预测详情分片，每个分片包含若干资源的完整预测数据。通过 `summary_index.json` 中的 `detail_ref` 引用。

### `forecast_error_report.json`

预测误差报告，按资源、指标、模型和窗口展开，输出 `rmse`、`mae`、`mape`、`p95_error` 等指标。`rows` 提供扁平记录，`resources` 提供按资源聚合的嵌套结构，便于报表、审计和模型效果对比。

### `generation_stats.json`

本次预测的统计信息：资源数、预测模型、窗口参数、耗时、输出大小、误差报告文件名等。

### 旧产物升级

新版本不读取、不迁移旧的单体 raw 产物。升级后应删除旧 scope 目录并重新生成：

```bash
rm -rf outputs/vm outputs/k8s
python generate_forecasts.py
```
