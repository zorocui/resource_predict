# 部署配置与输出结构

本文档详细说明系统的部署配置文件、参数设置和预测产物输出结构。

## 配置文件概览

| 文件 | 用途 | 是否提交 Git |
| --- | --- | --- |
| `resource_predict/settings.py` | 页面启动前必须确定的监听、目录和日志设置 | 是 |
| `deploy/runtime_config.json` | 页面统一管理的数据采集、预测和决策运行配置 | 是 |
| `deploy/clusters.json` | VM / K8S 调配集群配置（含 SSH 凭据） | 否 |
| `deploy/k8s_prometheus_clusters.json` | K8S Prometheus 集群地址与认证 | 否 |
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

## 预测模型配置（`deploy/runtime_config.json` 的 `prediction` 段）

预测模型开关由统一运行配置管理，可在 Web 的“系统配置”页面 →“预测配置”分区中修改，
保存后写入 `deploy/runtime_config.json` 并立即对新任务生效：

```json
{
  "prediction": {
    "vm_test_duration": "72h",
    "vm_future_duration": "24h",
    "workload_test_duration": "24h",
    "workload_future_duration": "24h",
    "enabled_methods": ["seasonal_naive", "prophet"],
    "enable_ensemble": false
  }
}
```

| 字段 | 作用 |
| --- | --- |
| `enabled_methods` | 参与竞选的候选模型，取值 `arima` / `sarima` / `prophet` / `seasonal_naive` / `rolling_mean`，至少一个。 |
| `enable_ensemble` | `true` 表示在至少两个模型完成验证时生成集成候选。独立测试和未来预测的权重只来自训练段内验证分数；首个验证折等权，后续验证折使用此前折的分数。 |

以下开关是 `resource_predict/internal_settings.py` 中 `ForecastConfig` 的代码级默认值，
不通过页面或配置文件暴露，需要调整时直接改代码：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `reuse_backtest_model_for_future` | `False` | 已停用的兼容读取字段，旧输入 `True` 也不会启用延伸预测。未来预测始终用最新完整历史重新拟合。 |
| `archive_enabled` | `True` | 是否将本轮新生成的入选预测写入各 scope 的 `forecast_history/`。 |
| `archive_retention_days` | `7` | 留档保留天数，须为正数；成功写入非空批次后清理过期批次。代码级配置，不在页面暴露。 |
| `prophet_routing_enabled` | `True` | `True` 表示仅在轻量统计特征显示存在明显趋势或季节性时运行 Prophet。若 Prophet 是唯一启用模型，则仍会运行。 |
| `prophet_routing_mode` | `auto` | `auto` 使用自动路由规则，`always` 表示启用 Prophet 时总是运行，`never` 表示存在其他兜底模型时跳过 Prophet。 |
| `rolling_backtest_folds` | `1` | 训练段内的时间验证折数；每折长度等于 `test_size`，外层独立测试另计。多折时选型分数为 `0.65 × 最近验证折RMSE + 0.35 × 全部验证残差RMSE`。不足折数时记录实际折数；候选须完成全部可用折。 |
| `anomaly_route_zscore_threshold` | `3.5` | 近期鲁棒 z-score 超过该值时，最优选择收窄到 `ensemble` / `seasonal_naive` / `rolling_mean`。 |

旧版 `deploy/forecast_config.json` 已从仓库和工作区移除，预测流程也不再读取它。
`services/runtime_config.py` 仍保留一次性迁移逻辑：只有当 `deploy/runtime_config.json`
不存在、而升级前遗留的 `deploy/forecast_config.json` 还在时，才从中读取 `enabled_methods`
和 `enable_ensemble` 作为初始值。部署包不会打包该文件，因此全新部署不会触发迁移。

## 全局默认配置（`resource_predict/settings.py`）

`settings.py` 已精简为启动设置，只保留静态/模板/输出目录、日志和 Flask host/port/debug。
业务运行配置请在 Web 的“系统配置”页面修改，保存到 `deploy/runtime_config.json` 后立即对新任务生效。

页面运行配置只保留三组常用字段：数据采集（定时开关、周期、拉取历史、本地保留、步长、rate 窗口、超时、分片与重试）、
预测（VM/K8S 验证与预测窗口、候选模型、Ensemble）以及决策（策略等级、扩缩容阈值、确认轮次、
冷却时间和命名空间策略）。Prophet 底层参数、缓存、分页、mock 随机种子等实现细节不再作为用户配置。

保存时服务端先校验完整配置；任何字段或集群配置错误都会整体拒绝。调度配置变化会唤醒唯一的
K8S 后台调度线程重新读取开关和周期，不需要重启应用。

唤醒本身不等于拉取。调度循环只在一个条件下取数：到期时刻已经过去。到期时刻按
`last_start + max(60 秒, scheduled_update_interval_minutes)` 计算，`last_start` 是上一次拉取
**开始**时的时刻（拉取失败同样占用本轮，因此不会快速重试），从未拉取过时视为已到期。保存配置只是让
循环提前重新评估这个条件，于是有四种结果：

- 新的到期时刻仍在未来：不拉取，继续等待剩余时间，周期既不被重置也不被提前。
- 把周期改短到 `last_start + 新周期` 已经落在过去：立即拉取一轮。
- 关闭定时拉取后再打开：关闭时长不足一个周期则等到原到期时刻，超过则立即拉取一轮。
- 应用启动后从未拉取过（含线程启动时开关是关闭、之后才打开的情况）：立即执行首轮。

首轮以及此后任何一次成功拉取之前的重试都标记为 `scheduled_startup`，之后标记为 `scheduled`。
需要马上取数请显式调用 `POST /api/cluster-configs/k8s-fetch` 或页面上的拉取按钮。

计时锚定在**开始**时刻而不是完成时刻，是为了让两轮拉取的实际间隔严格等于配置周期。增量回看
窗口固定为 `scheduled_update_interval_minutes + incremental_overlap_minutes`（默认 360 + 60
= 420 分钟），只有实际间隔不超过这个窗口才不会留下永久取不到的时间段。按开始时刻计时时实际
间隔是 `max(周期, 拉取耗时)`，所以默认配置下拉取耗时不超过 420 分钟都是安全的；如果按完成
时刻计时，实际间隔会变成 `周期 + 拉取耗时`，耗时一旦超过 60 分钟的 overlap 就开始每轮漏数据，
漏掉的时长等于 `拉取耗时 - incremental_overlap_minutes`。拉取耗时超过配置周期时会记录一条
warning 并立即开始下一轮。

漏掉的时间段如果超过 `step_seconds × (max_interpolation_gap_steps + 1)`（默认 600 × 4 = 40
分钟），预测端会认为数据断档，只取最近一段连续数据；该段不足验证窗口时会跳过重算并沿用旧预测，
详见 [architecture.md](architecture.md) 的预测数据可用性说明。
### 数据采集与本地保留

`deploy/runtime_config.json` 的 `collection` 段包含以下 Prometheus 采集参数：

```json
{
  "collection": {
    "scheduled_update_enabled": true,
    "scheduled_update_interval_minutes": 360,
    "history_days": 7,
    "retention_days": 30,
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
| `history_days` | `7` | K8S Prometheus 首次或全量拉取的历史范围；不是本地数据保留期 |
| `retention_days` | `30` | 本地 raw 监控数据保留窗口；VM、Workload 汇总及 Container 序列均按各自最新有效样本向前保留 30 天 |
| `range_query_chunk_hours` | `24` | `query_range` 单个时间分片的最大时长（小时） |
| `request_max_attempts` | `3` | 每个 Prometheus HTTP 请求的最大尝试次数，包含首次请求 |
| `retry_backoff_seconds` | `1.0` | 首次重试等待秒数；后续按指数退避增长 |
| `max_interpolation_gap_steps` | `3` | 只对不超过该采样步数的完整内部缺口用此前观测向前填补；更大的缺口保持断开 |

连接失败、超时、HTTP 429 和 5xx 会在最大尝试次数内重试；其他 4xx 参数或认证错误不会重试。`query_range` 的分片结果按完整 Prometheus 标签集合与时间戳合并，并在边界时间戳重复时保留后一个分片的样本。任一分片在重试耗尽后失败，会使该集群的本轮查询整体失败，不会提交不完整的时间范围。

HTTP 层重试之外还有一层整轮重试：某个集群查询成功但聚合不出任何 Workload 时（容器使用率序列为空、`kube_pod_owner` 瞬时查询无结果或失败、owner 标签不匹配、CPU 与内存序列无法配对），会按 15 秒、30 秒退避重拉该集群，最多 3 次尝试（`AGGREGATION_MAX_ATTEMPTS`，不通过页面配置），全部失败才记为该集群 `failed`。这类失败多数是 kube-state-metrics 重启、Prometheus 刚启动或查询限流造成的临时现象，因为 `kube_pod_owner` 走的是只回看约 5 分钟的瞬时查询，而 CPU/内存走的是数小时的 range 查询，两者可用性并不同步。

错误消息会写明具体断点和序列计数，不再统一报“未返回可聚合的 K8S Workload”。日志中每轮拉取都会输出一行 `fetch target aggregation`，包含 `cpu_series`、`memory_series`、`container_series`、`pod_owner_rows`、`replicaset_owner_rows`、`workloads_resolved`、`orphan_container_series` 以及 owner 查询异常原文，可直接用于区分监控侧临时缺数据和配置或标签不匹配。`kube_pod_owner`、`kube_replicaset_owner`、request/limit 与副本数这些瞬时查询失败时会各自记录 warning，不再被静默吞掉。

同一轮中其他成功集群仍会按 `resource_id` 增量 upsert。本轮未返回或失败集群所属的 Workload 不会被当作删除，也不会清空已有 raw 历史和预测产物。采集端仅补齐短缺口；预测端使用最近连续且足够长的数据段，连续段不足测试窗口时跳过该指标或 Workload 的本轮重算并保留旧预测。

增量合并后，系统会使用 `retention_days` 按时间戳裁剪每条序列，保留大于或等于“该序列最新时间戳减 30 天”的样本。不规则采样和缺口不会改用点数估算；暂时离线的资源保留最后已知的有界 30 天窗口，不按墙上时间整体删除。

历史默认配置组（仅供理解内部默认值，不再由用户直接编辑）：

| 配置类 | 关键参数 | 默认值 |
| --- | --- | --- |
| `AppConfig` | `host` / `port` / `out_dir` / `log_file` / `debug` | `0.0.0.0` / `5000` / `outputs` / `resource_predict.log` / `False` |
| `GenerationConfig` | `default_test_size` / `default_future_steps` / `freq` / `detail_chunk_size` / `detail_history_points_default` / `detail_history_points_max` / `raw_resource_cache_items` | `72` / `24` / `h` / `25` / `1000` / `10000` / `100` |
| `ForecastConfig` | `enabled_methods` / `enable_ensemble` / `rolling_backtest_folds` / `reuse_backtest_model_for_future` / `prophet_routing_enabled` / `prophet_routing_mode` / `anomaly_route_zscore_threshold` | `("seasonal_naive", "prophet")` / `False` / `1` / `False` / `True` / `auto` / `3.5` |
| `DecisionConfig` | `scale_out_threshold` / `scale_in_threshold` / `scale_in_max_reduction_ratio` / `scale_out_confirmations` / `scale_in_confirmations` / `action_gate_state_retention_days` | `0.8` / `0.2` / `0.5` / `2` / `3` / `30` |
| `UpdateConfig` | `enabled` / `interval_minutes` / `startup_delay_seconds` / `sliding_window` | `False` / `60` / `60` / `False` |
| `K8SPrometheusConfig` | `history_days` / `incremental_overlap_minutes` / `step_seconds` / `rate_window` / `scheduled_update_enabled` / `scheduled_update_interval_minutes` / `range_query_chunk_hours` / `request_max_attempts` / `retry_backoff_seconds` / `max_interpolation_gap_steps` | `7` / `60` / `600` / `15m` / `True` / `360` / `24` / `3` / `1.0` / `3` |

`rate_window` 会用于真实 CPU usage 查询中的 `rate(container_cpu_usage_seconds_total[...])` 窗口；未在集群配置中指定时使用全局默认值 `15m`。默认 `step_seconds=600` 表示每 10 分钟返回一个结果点，两个参数彼此独立。

K8S Prometheus 首次接入、本地 K8S raw 数据缺失或 API 传入 `full_refresh=true` 时，会按 `history_days` 拉取全量历史窗口（默认最近 7 天）。已有本地基线后的普通拉取会使用增量窗口：`scheduled_update_interval_minutes + incremental_overlap_minutes`，默认 `360 + 60 = 420` 分钟，即最近 7 小时。
这两个拉取窗口都与本地 `retention_days=30` 保留窗口独立。

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

新生成预测的基础误差来自外层独立测试；`validation_*` 是训练段内部验证误差，`selection_rmse` 只使用验证数据。缺少验证历史时不根据测试误差选择模型，标记 `insufficient_validation_history`；历史充足但验证全部失败标记 `validation_failed`。降级优先采用已配置的 Seasonal Naive、Rolling Mean，否则按配置顺序；在线预测失败再使用 Rolling Mean。

报告包含容器维度（聚合指标的 `container` 为 `null`），实际测试时间边界、评估角色和来源。失败模型保留失败原因与空误差；不能把空误差当作零。旧产物缺少来源时标记为 `legacy_holdout`，不追认独立测试。`p95_error` 是绝对误差的分位值；未来曲线 P95 和规则 `confidence_score` 都不是统计预测覆盖率。

### `forecast_history/forecast_<timestamp>_<uuid>.jsonl.gz`

每轮按 scope 保存独立 gzip JSONL 文件，一行一个新预测资源，含 `forecasts`、`container_forecasts`、容器规格、数据质量和当轮原始建议。每条预测仅保存入选模型、未来时间轴、预测值和 `provenance`，不重复保存训练曲线或全部候选模型。

`provenance` 包含 `generated_at_epoch_ms`、`data_end_ms`、`train_end_ms`、`forecast_start_ms`、`forecast_end_ms`、`config_hash`、`model_version`、`actual_future_methods`、`ensemble_members`。模型降级时入选名称和实际曲线名称一致，原选型记录在诊断中。

留档发生在旧预测恢复、增量合并和跨轮次 action-gate 确认之前，因此仅记录本轮实际生成的预测，建议不是最终执行凭证。失败的批次不会发布半个文件；留档失败不阻止预测产物更新，日志与 `generation_stats.json.forecast_archive` 记录状态。真实值评分见下节；尚未实现概率区间校准。

### `forecast_realized.sqlite3` / `forecast_realized_report.json`

各 scope 的 raw 提交后、预测留档后自动回填历史预测误差。SQLite 的 `batches`、`curves`、`points` 分别保存导入批次、入选曲线及逐点评分，唯一键避免重复导入和计分。首次真实评分保留不变，迟到数据可以补评未评分点。只读取未导入的压缩批次，按资源和指标索引匹配，不把全部历史加载到内存。

性能优化后，raw 提交只持久化评分及执行保留期清理，延迟生成 JSON 汇总；预测结束或显式评分 CLI 才发布报告，避免一轮两次扫描完整账本。若 raw 已提交但后续预测失败，数据库评分仍有效，报告可能较旧，可运行下方 CLI 更新。返回状态 `report_published=false`、`coverage=null` 表示本次未汇总，不是零覆盖。`generation_stats.json.forecast_realized` 记录导入、评分、保留期/报告、校准和影子建议生成的阶段耗时。

评分必须有 `observation_evidence`（schema_version=1）：`source`、`resource_type`、`spec`、`container_metric_modes`、`metrics` 和 `container_metrics`。两种指标映射中的每个指标使用 `timestamps`（整数毫秒）和 `values` 数组，只能包含未填补的真实采样。K8S provider 自动生成同频聚合的真实采样证据；证据以独立规格快照写入 raw 分片。VM/自定义 provider 需显式提供此契约；mock 和无证据的旧 raw 不追认为真实观测。证据当前仅保留最近接收批次，连续多轮评分失败后的完整恢复需要重新提供遗漏批次。

目标时间必须精确匹配且已到达，并严格晚于数据截止及留档批次开始时间/模型生成时间的较大值。缺失来源、非有限观测、规格、口径或 K8S 观测成员变化均不计分。K8S 规格来自拉取时快照，仍无法证明整个历史窗口规格不变；该限制适用于跨规格变更的实验。

JSON 报告 `evaluation_role=realized_selected_forecast`，按模型、指标、单位口径、资源/容器层级和数据截止提前量分组（0–1h、1–6h、6–24h、>24h），输出 `count`、`mae`、`rmse`、`underestimate_rate`、`mean_underestimate`。最后一项是所有评分点的 `max(actual-predicted,0)` 均值。`coverage` 区分 scored、awaiting_target、awaiting_observation、missing_provenance、not_future_at_publication、nonfinite_observation、basis_mismatch；未评分不是零误差。仅统计当时入选的模型，不能据此公平比较所有候选。

保留期沿用 `archive_retention_days`（默认 7 天），按批次时间清理账本记录。SQLite 空闲页复用，文件不会每轮缩小；该周期之外的迟到数据不再补评。日志和 `generation_stats.json.forecast_realized` 记录评分状态，失败不撤销已提交 raw/预测。账本可查询预测来源、原预测值、真实值、评分时间，以及 `target_ms-data_end_ms` 和 `target_ms-issued_ms` 两种提前量；`issued_ms` 是保守批次时间代理，并非前端实际可见时间。

重试（按 scope 分别运行）：

```bash
python -m resource_predict.pipeline.realized_error --out-dir outputs/k8s
```

### 经验预测上界（观察阶段）

新预测图表的 `calibration` 保存与 `x_pred_ms` 对齐的 `upper`，资源和容器详情 API 均保留该字段。`scaling_advice.prediction_upper_bound` 给出指标上界峰值及 `complete`；`mode=observe`、`applied_to_targets=false` 表示仅用于观察，现有规格目标、action、confidence 和执行门控继续使用原策略。上界不等于预测曲线自身的 P95，也不是规则置信分数。

按同一资源、容器、指标、模型、模型版本、配置哈希、规格和单位口径收集预测生成前已经评分的真实残差 `actual-predicted`，目标时间必须不晚于当前数据截止。提前量分组沿用 0–1h、1–6h、6–24h、>24h；同一目标时间仅取最后发布的可比预测。每组取最近最多 500 个目标、至少需要 60 个；使用第 `ceil((n+1)*0.95)` 个有序残差作为余量，下限为 0。预测上界为入选预测加余量，不截断到 100%。这些是代码级固定基线，未新增页面配置。

`status` 为 calibrated、partial、insufficient_samples、missing_provenance 或 failed；样本不足的点 `upper=null`，不能按零解释，也不能把部分时段的峰值当成完整窗口上界。`buckets` 记录样本数、余量、样本时间范围和摘要，基准时间与口径随上界保存。增量合并保留旧指标原上界；当前规格不再匹配时，建议摘要标记 basis_changed 并隐藏峰值。

上界随原曲线留档，SQLite 增加 `calibrations`（参数 JSON）和 `upper_bounds`（逐点原上界），旧库自动增表。`forecast_realized_report.json.calibration_rows` 按模型、指标、层级、单位和提前量统计 `count`、`empirical_coverage`（actual≤upper 的比例）、`mean_exceedance`（max(actual-upper,0) 的均值）、`mean_margin`（upper-predicted 的均值）。仅核验当时留档的上界，不事后重算；旧预测没有上界时不追补覆盖记录。

95% 是经验校准目标，尚非生产实测覆盖保证；时序相关、业务漂移及 K8S 历史规格证据的限制仍适用。严格分组可能长期样本不足，这是保留原策略的正常状态。当前还不能据此声称降低资源预留或费用。

### 影子建议对照

资源详情与摘要增加 `shadow_comparison`（version=1、mode=shadow、executable=false）。校准完整时 `status=paired`，冻结 `baseline` 和 `candidate` 的 action、target_spec、policy_tier；candidate 复用现有建议算法，把输入换成校准上界。baseline 是跨轮次确认之前的建议快照，`baseline_stage=before_cross_run_confirmation`，不是最终执行授权。正式建议及其门控不读取 candidate。

只有全部指标、K8S 全部已知容器都具有完整且同口径的校准上界才生成对照。缺样本、部分校准、局部重算或缺容器时 `status=unavailable` 并记录 reason。对照保存 source_spec、forecast_windows 和 budgets；当前规格变化时，旧对照不作为当前有效方案展示。旧留档不追补影子结果。

SQLite 增加 `shadow_runs`（每批资源的两套快照）和 `shadow_budgets`（对应曲线的资源量和比例），和原批次一起原子导入、去重、按 archive_retention_days 级联清理。真实报告增加 `shadow_comparison`：

| 字段 | 含义 |
| --- | --- |
| run_counts / unavailable_reasons | 有效配对数、不可用及其原因；缺样本不是零误差 |
| allocation_rows | 按资源类型、指标、单位和 role 统计建议配对数量、两套资源量均值与差值；一条曲线只计一次，不按预测点重复累计 |
| actual_rows | 在完全相同的已评分观测点上，统计两套方案的超出率和平均超出比例；matched_points 是分母 |
| budget_skip_reasons | 缺规格、非比值口径、副本数变化等不能评分的原因 |
| change_rows | 相邻同资源同规格有效建议之间的 action/target_spec/policy_tier 变更次数和比例；不可用轮次切断比较 |

VM 比较目标 CPU/内存/磁盘规格；K8S 在容器粒度分别比较 request 和 limit，建议资源量为每副本规格乘目标副本数。`role=request_budget` 的超出表示预留预算不足，不等同于实际容量故障。只有两套方案副本数都等于观测副本数、且指标为比值口径时，才用真实值评分；副本变化仍展示资源量差异，但标记 replicas_changed。其他成员或口径变化沿用真实误差回填的可比性检查。

这里的超出幅度是相对原规格的归一化值，不是 cores/GiB；allocation_rows 才使用实际资源单位。结果假设资源需求保持观测值不变，不模拟限流、OOM、调度、执行延迟及负载再分配，也不代表 SLA 或费用收益。现有算法的固定余量仍保留，校准方案不保证比原策略省资源；必须等后续真实数据再判断效果。

### 校准策略启用判定（仅供评审）

`forecast_realized_report.json.activation_assessment` 按单个资源评估完整指标集合，`mode=review_only`、`automatic_activation=false`，不修改正式建议或执行授权。`status=no_shadow_evidence` 表示尚无影子轮次；否则 `resources[]` 中每个资源输出 continue_observing（继续观察）或 eligible_for_review（具备启用评审条件）。判定不是已经启用，也不是统计安全保证。

规则版本为 `empirical_review_v1`，阈值存于报告 `rules` 和 `pipeline/activation_assessment.py`，当前没有页面开关：

| 条件 | 默认要求 |
| --- | --- |
| 连续可比影子轮次 | 至少 12 轮；不可用轮次、规格/口径、policy tier、模型/版本/配置变化后重新积累 |
| 建议相邻转换 | 至少 10 次，candidate 的变更率不得高于 baseline |
| 每个指标真实样本 | 至少 100 个不同目标时间，跨度至少 72 小时 |
| 已到期点观测完整率 | 至少 95%；先按最新发布预测去重，再判断是否有真实评分 |
| 新鲜度 | 最新预测发布时间、数据截止时间、各指标最新真实目标均不超过 24 小时 |
| 每个指标风险 | 超出比例及平均超出幅度不得增加 |
| 每个指标分配量 | 同一组真实匹配点上的平均分配量不得增加 |
| 预留收益 | 至少一个维度减少 5%；K8S 只把 request 的减少计为预留收益，limit 单独减少不算 |

K8S 全部容器的 request/limit 必须通过检查；某一指标失败不能由其他容器的改善抵消。缺容量、副本变化或缺来源等无法比较的最新指标会阻止判定，`budget_skip_reasons` 和 `missing_metrics` 提供原因。资源和容器之间不合并样本。

`resources[]` 保存最新/最早连续批次、有效轮数、稳定性统计、逐指标 due_targets/matched_targets、观测完整率、时间范围、两套方案风险与分配量、checks/failed_checks 和资源级 reasons。去重时最新曲线尚无观测的目标仍计入待评分母，不拿旧曲线的已评分结果代替。所有风险和收益检查使用同一组真实配对点；仅对浮点累计采用 `1e-12` 数值容差。

判定带 generated_at_epoch_ms 和每资源 valid_until_epoch_ms。消费旧报告时必须核对截止时间，并重新检查当前规格/配置是否仍匹配；新数据或策略变化需要重新生成报告。原 raw 提交仍只评分，判定随预测结束或评分 CLI 生成报告时计算。阈值是保守工程判据，样本时序相关性、历史规格证据、离线反事实及真实执行影响等限制仍然存在；具备评审条件不意味着已证明生产 SLA 或节省费用。

### 按资源受控采用校准建议

`internal_settings.py` 的 `DecisionConfig` 新增代码级参数（不在页面或 runtime_config.json 暴露）：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| calibrated_advice_enabled | False | 是否允许受控采用校准建议 |
| calibrated_advice_resource_ids | () | 明确允许的资源 ID；开关打开但列表为空仍不会采用 |

本版本默认保持观察模式，没有自动选资源或自动启用 API。运维评审后可修改以上代码级配置并重启；本次实现未为任何资源开启配置。ID 必须是具体资源标识，不支持通配符。

只有本轮完整新预测、同规格的 paired 影子结果、成功留档、成功发布的当前批次报告，以及仍有效的 eligible_for_review 判定全部匹配，才采用校准建议。重新计算整套 action、target_spec、confidence 和 K8S 策略，并核对其关键字段与本轮冻结 candidate 一致。不会把影子对象直接当作执行授权。

正式 `scaling_advice.calibration_activation.status=active` 表示已采用；元数据包含报告路径、批次、有效期、规格/口径和策略摘要、目标摘要，以及供回退的完整 baseline_advice。`prediction_upper_bound.applied_to_targets=true`、mode=active 同步标注。失败时 status=baseline 并记录 reason。配置关闭、移出列表、非本轮预测、局部重算、过期/旧报告或策略不匹配均在下一次预测流程回退原建议；期间旧校准建议不能通过新增执行检查。

基线/校准切换或校准策略配置改变时，action_gate 的跨轮次确认从第 1 轮重新计数。确认账本新增 strategy 字段，旧记录按 baseline 读取。原有 action_gate、confidence、data_quality、cooldown、policy_tier 等门控保留；非手工覆盖的 execute 入队、任务开始和首条远程命令之前还会复核校准配置、来源、目标、期限及当前账本判定。raw 已评分但报告未更新时，会从 SQLite 重新评估该资源，失效则拒绝并要求重新预测。

影子留档仍冻结切换之前的 baseline/candidate，保持对照实验定义；实际采用情况保存在当前正式建议元数据，执行任务继续记录实际计划。本功能不创建扩缩容任务，不部署生产策略，不证明资源收益；缺少足够证据时即使开关打开也继续使用基线。

### `generation_stats.json` 统计内容

本次预测的统计信息：资源数、预测模型、窗口参数、耗时、输出大小、误差报告文件名等。

### 旧产物升级

新版本不读取、不迁移旧的单体 raw 产物。升级后应删除旧 scope 目录并重新生成：

```bash
rm -rf outputs/vm outputs/k8s
python generate_forecasts.py
```
