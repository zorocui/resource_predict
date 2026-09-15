# 部署配置与输出结构

本文档详细说明系统的部署配置文件、参数设置和预测产物输出结构。

## 评分口径与产物升级（v2）

紧急度与置信度均显示中文等级和 `分数/100`，属于规则评分，不能解释为概率。紧急度默认低<40、中40–<70、高70–<90、容量风险≥90为紧急（节省机会≥90为极高）；阈值尚未经生产回放校准。置信度继续使用低<45、中45–<72、高≥72的门控边界。

新预测生成 `scaling_advice.confidence_breakdown`（版本、满分、总分、实际加减项）；K8S 同时保存 `container_advice` 及有效预测 `sample_count`，供容器级评分和解释使用。缺预测数据不作为零负载缩容；质量、基线扣分在合并后应用；混合方向最高71分。既有执行门控仍保留。

紧急度在列表和详情 API 中即时按 v2 计算；旧置信度产物不会自动改写，重新运行预测后获得新版结果和分解。前端遇到旧分解只显示已保存分数，不能据此推断新版公式。完整公式见 [architecture.md](architecture.md#置信度评分v2)，字段说明见 [api-reference.md](api-reference.md#评分字段v2)。

## 配置文件概览

Python3.10可以链接旧SQLite；项目现已兼容原生SQLite3.7.17，不安装替代驱动。若此前准确性查询因deterministic或窗口函数失败，更新代码后保留原库即可。见 [sqlite-compatibility.md](sqlite-compatibility.md)。

调配成效独立保存于各类型输出目录的 `scaling_effects.sqlite3`，不受任务JSON最近1000条限制。默认政策版本2：调配后下一次有效采集确认生效，即与调配前最近采样对比并计入汇总，不等待稳定期或24小时窗口；单点不计算累计容量时。缺少数据则继续等待采集，最长补齐7天。旧版默认窗口的未完成事件随采集升级，已评估历史保持冻结；来源契约、采样要求与存储边界见 [scaling-effects.md](scaling-effects.md)。

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
| `enabled_methods` | 参与竞选的候选模型，取值 `arima` / `sarima` / `prophet` / `seasonal_naive` / `rolling_mean` / `lstm`，至少一个。 |
| `lstm_model_path` | 离线 model.pt 路径；默认空，启用 lstm 时必填。在线只加载推理，支持训练产物 v2/v3。 |
| `lstm_max_age_hours` | 最大训练滞后小时数，默认 168，0 不限制；过期使该候选失败，不触发在线训练。 |
| `enable_ensemble` | `true` 表示在至少两个模型完成验证时生成集成候选。独立测试和未来预测的权重只来自训练段内验证分数；首个验证折等权，后续验证折使用此前折的分数。 |
| `parallel_backend` | 默认 `auto`，可选 `process`/`thread`/`serial`；auto对重模型选择多进程，轻量模型选择线程。 |
| `max_workers` | 默认 `0` 自动按可用CPU规划；1–256为显式上限，实际受CPU配额和任务数约束。 |

并行配置在“系统配置 → 预测配置”保存，下一批生效；虚拟机可用CPU每轮重新识别，容器和聚合指标共用有界任务池。CLI覆盖、内存边界和吞吐基准见 [parallel-prediction.md](parallel-prediction.md)。

以下开关是 `resource_predict/internal_settings.py` 中 `ForecastConfig` 的代码级默认值，
不通过页面或配置文件暴露，需要调整时直接改代码：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `reuse_backtest_model_for_future` | `False` | 已停用的兼容读取字段，旧输入 `True` 也不会启用延伸预测。未来预测始终用最新完整历史重新拟合。 |
| `prophet_routing_enabled` | `True` | `True` 表示仅在轻量统计特征显示存在明显趋势或季节性时运行 Prophet。若 Prophet 是唯一启用模型，则仍会运行。 |
| `prophet_routing_mode` | `auto` | `auto` 使用自动路由规则，`always` 表示启用 Prophet 时总是运行，`never` 表示存在其他兜底模型时跳过 Prophet。 |
| `rolling_backtest_folds` | `3` | 训练段内的时间验证折数；每折长度等于 `test_size`，外层独立测试另计。利用率按汇总容差达标率优先，相同时比较 RMSE；绝对使用量继续按 RMSE。多折 RMSE 分数为 `0.65 × 最近验证折RMSE + 0.35 × 全部验证残差RMSE`。不足折数时记录实际折数；候选须完成全部可用折。增加折数会增加验证计算量。 |
| `anomaly_route_zscore_threshold` | `3.5` | 近期鲁棒 z-score 超过该值时，最优选择收窄到 `ensemble` / `seasonal_naive` / `rolling_mean`。 |

旧版 `deploy/forecast_config.json` 已从仓库和工作区移除，预测流程也不再读取它。
`services/runtime_config.py` 仍保留一次性迁移逻辑：只有当 `deploy/runtime_config.json`
不存在、而升级前遗留的 `deploy/forecast_config.json` 还在时，才从中读取 `enabled_methods`
和 `enable_ensemble` 作为初始值。部署包不会打包该文件，因此全新部署不会触发迁移。

## 全局默认配置（`resource_predict/settings.py`）

已训练 LSTM 的身份匹配、离线验证标签隔离、模型缓存和兜底规则见 [lstm-online.md](lstm-online.md)。

`settings.py` 已精简为启动设置，只保留静态/模板/输出目录、日志和 Flask host/port/debug。
业务运行配置请在 Web 的“系统配置”页面修改，保存到 `deploy/runtime_config.json` 后立即对新任务生效。

页面运行配置只保留三组常用字段：数据采集（定时开关、周期、拉取历史、本地保留、步长、rate 窗口、超时、分片与重试）、
预测（VM/K8S 验证与预测窗口、候选模型、Ensemble）以及决策（策略等级、扩缩容阈值、确认轮次、
冷却时间和命名空间策略）。Prophet 底层参数、缓存、分页、mock 随机种子等实现细节不再作为用户配置。

保存时服务端先校验完整配置；任何字段或集群配置错误都会整体拒绝。应用启动时保留 K8S 定时调度线程，调度配置变化会唤醒该线程重读开关和周期。保存配置不额外触发拉取。

唤醒本身不等于拉取。调度循环只在一个条件下取数：到期时刻已经过去。到期时刻按
`last_start + max(60 秒, scheduled_update_interval_minutes)` 计算，`last_start` 是上一次拉取
**开始**时的时刻（拉取失败同样占用本轮，因此不会快速重试），重启恢复原计时基准；没有调度状态或自动拉取历史的首次接入才以启动时刻为基准。保存配置只是让
循环提前重新评估这个条件，于是有四种结果：

- 新的到期时刻仍在未来：不拉取，继续等待剩余时间，周期既不被重置也不被提前。
- 把周期改短到 `last_start + 新周期` 已经落在过去：立即拉取一轮。
- 关闭定时拉取后再打开：关闭时长不足一个周期则等到原到期时刻，超过则立即拉取一轮。
- 应用重启后沿用原周期，未到期继续等待，已过期补跑一轮；无历史的首次接入等待完整周期。

定时拉取统一标记为 `scheduled`。
调度基准保存在输出根目录的 `k8s_scheduler_state.json`，每次自动任务开始前原子写入，手动拉取不改写。首次升级没有状态文件时，从 `update_history.json` 恢复最近一次后台自动拉取的开始时间，忽略手动记录。部署迁移需保留这两个文件。例如自动任务05:48开始、周期6小时，重启后仍等到11:48；若12:00才启动则立即补跑一次，下一轮按补跑开始时间计时，不逐个补跑停机期间的过期轮次。状态损坏时记录日志并尝试历史恢复；保存失败时当前进程仍运行，但不能保证下次重启恢复。
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
| `ForecastConfig` | `enabled_methods` / `enable_ensemble` / `rolling_backtest_folds` / `reuse_backtest_model_for_future` / `prophet_routing_enabled` / `prophet_routing_mode` / `anomaly_route_zscore_threshold` | `("seasonal_naive", "prophet")` / `False` / `3` / `False` / `True` / `auto` / `3.5` |
| `DecisionConfig` | `scale_out_threshold` / `scale_in_threshold` / `scale_in_max_reduction_ratio` / `scale_out_confirmations` / `scale_in_confirmations` / `action_gate_state_retention_days` | `0.8` / `0.2` / `0.5` / `2` / `3` / `30` |
| `UpdateConfig` | `enabled` / `interval_minutes` / `sliding_window` | `False` / `60` / `False` |
| `K8SPrometheusConfig` | `history_days` / `incremental_overlap_minutes` / `step_seconds` / `rate_window` / `scheduled_update_enabled` / `scheduled_update_interval_minutes` / `range_query_chunk_hours` / `request_max_attempts` / `retry_backoff_seconds` / `max_interpolation_gap_steps` | `7` / `60` / `600` / `15m` / `True` / `360` / `24` / `3` / `1.0` / `3` |

`rate_window` 会用于真实 CPU usage 查询中的 `rate(container_cpu_usage_seconds_total[...])` 窗口；未在集群配置中指定时使用全局默认值 `15m`。默认 `step_seconds=600` 表示每 10 分钟返回一个结果点，两个参数彼此独立。

K8S Prometheus 首次接入、本地 K8S raw 数据缺失或 API 传入 `full_refresh=true` 时，会按 `history_days` 拉取全量历史窗口（默认最近 7 天）。已有本地基线后的普通拉取会使用增量窗口：`scheduled_update_interval_minutes + incremental_overlap_minutes`，默认 `360 + 60 = 420` 分钟，即最近 7 小时。

窗口按集群分别判断，适用于单集群、批量和后台定时拉取：即使已有其他集群的 raw 数据，新集群仍使用完整历史窗口，已有集群继续增量。若新集群已受旧逻辑影响而只保存了增量数据，可在监控集群所在行点击“全量拉取”补齐历史；页面调用拉取 API，仅提交该集群并传入 `full_refresh=true`，其他集群不参与本次拉取和预测。
这两个拉取窗口都与本地 `retention_days=30` 保留窗口独立。

通过 `python app.py` 启动时会启动 K8S 定时调度线程。启用定时拉取后，仍按 `scheduled_update_interval_minutes`（默认 360 分钟，即 6 小时）执行；仅取消启动时额外拉取的那一轮，并移除启动拉取延迟配置。VM 数据更新仍需通过页面按钮、API 或 CLI 手动触发。

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

### `forecast_accuracy_summary.json`

每次预测保存本轮实际选用模型的独立历史测试汇总：资源、容器、指标、模型、有效点、无效点、达标点、准确率、MAE和测试时间。只保留最新一轮，不累积逐点记录。口径与使用说明见[预测准确率](forecast-accuracy.md)。逐点留档和SQLite导入入口已删除，旧证据只读分析不再自动补入新数据。

### `generation_stats.json` 统计内容

本次预测的统计信息：资源数、预测模型、窗口参数、耗时、输出大小、误差报告文件名等。

### 旧产物升级

新版本不读取、不迁移旧的单体 raw 产物。升级后应删除旧 scope 目录并重新生成：

```bash
rm -rf outputs/vm outputs/k8s
python generate_forecasts.py
```
