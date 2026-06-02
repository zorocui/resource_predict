# 部署配置与输出结构

本文档详细说明系统的部署配置文件、参数设置和预测产物输出结构。

## 配置文件概览

| 文件 | 用途 | 是否提交 Git |
| --- | --- | --- |
| `resource_predict/settings.py` | 全局默认配置（frozen dataclass 单例） | 是 |
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
    "rate_window": "5m"
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

所有配置均为 frozen dataclass，运行时只读。主要配置组：

| 配置类 | 关键参数 | 默认值 |
| --- | --- | --- |
| `AppConfig` | `host` / `port` / `out_dir` / `log_file` / `debug` | `0.0.0.0` / `5000` / `outputs` / `resource_predict.log` / `False` |
| `GenerationConfig` | `default_test_size` / `default_future_steps` / `freq` | `72` / `24` / `h` |
| `ForecastConfig` | `enabled_methods` / `enable_ensemble` / `rolling_backtest_folds` / `reuse_backtest_model_for_future` / `prophet_routing_enabled` / `prophet_routing_mode` / `anomaly_route_zscore_threshold` | `("seasonal_naive", "prophet")` / `False` / `1` / `True` / `True` / `auto` / `3.5` |
| `DecisionConfig` | `scale_out_threshold` / `scale_in_threshold` / `scale_in_max_reduction_ratio` / `scale_out_confirmations` / `scale_in_confirmations` | `0.8` / `0.2` / `0.5` / `2` / `3` |
| `UpdateConfig` | `enabled` / `interval_minutes` / `sliding_window` / `display_window_points` | `False` / `60` / `False` / `0` |
| `K8SPrometheusConfig` | `history_days` / `incremental_overlap_minutes` / `step_seconds` / `rate_window` / `scheduled_update_enabled` / `scheduled_update_interval_minutes` | `7` / `60` / `300` / `5m` / `False` / `360` |

`rate_window` 会用于真实 CPU usage 查询中的 `rate(container_cpu_usage_seconds_total[...])` 窗口；未在集群配置中指定时使用全局默认值。

K8S Prometheus 首次接入、本地 K8S raw 数据缺失或 API 传入 `full_refresh=true` 时，会按 `history_days` 拉取全量历史窗口（默认最近 7 天）。已有本地基线后的定时/普通拉取会使用增量窗口：`scheduled_update_interval_minutes + incremental_overlap_minutes`，默认 `360 + 60 = 420` 分钟，即最近 7 小时。

### 预测窗口配置说明

| 配置 | 作用 |
| --- | --- |
| `default_test_size` / `default_future_steps` | 未设置资源族专用窗口时的兜底点数 |
| `vm_test_duration` / `vm_future_duration` | VM 专用时长，优先于点数 |
| `workload_test_duration` / `workload_future_duration` | K8S Workload 专用时长，默认 `24h` |

时长配置根据真实采样间隔自动换算点数。例如 `step_seconds=300` + `workload_test_duration="24h"` = 288 个测试点。

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
│   ├── raw_data.json          # 原始观测数据
│   ├── summary_index.json     # 资源列表摘要（含扩缩容建议）
│   ├── manifest.json          # 完整预测结果（含 charts）
│   ├── forecast_error_report.json # 预测误差报告
│   ├── generation_stats.json  # 本次生成统计
│   ├── backups/               # raw_data.json 历史备份
│   │   └── raw_data.20260530-120000.json
│   └── details/               # 详情分片
│       ├── part-00000.json
│       └── ...
├── k8s/
│   ├── raw_data.json
│   ├── summary_index.json
│   ├── manifest.json
│   ├── forecast_error_report.json
│   ├── generation_stats.json
│   ├── backups/               # raw_data.json 历史备份
│   └── details/
│       └── ...
└── scaling_tasks.json         # 调配任务记录
```

## 各文件说明

### `raw_data.json`

原始观测数据，是预测的唯一输入。

```json
{
  "meta": {
    "schema_version": 1,
    "saved_at_epoch_ms": 1717000000000,
    "freq": "h"
  },
  "resources": [
    {
      "resource_id": "vm-prod-001",
      "resource_type": "openstack_vm",
      "spec": {"cluster": "...", "instance_id": "...", "cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
      "metrics": {
        "cpu":    {"timestamps": [1717000000000, ...], "values": [0.45, ...]},
        "memory": {"timestamps": [...], "values": [...]},
        "disk":   {"timestamps": [...], "values": [...]}
      }
    }
  ]
}
```

### `summary_index.json`

资源列表摘要，包含扩缩容建议、紧急度、预测方法选择和 anomaly_score。前端列表页直接读取此文件。

### `manifest.json`

完整预测结果，包含合并后的 charts（y_train + y_test + 预测值 + 未来预测）。API 详情接口优先读取此文件。

### `details/part-*.json`

预测详情分片，每个分片包含若干资源的完整预测数据。通过 `summary_index.json` 中的 `detail_ref` 引用。

### `forecast_error_report.json`

预测误差报告，按资源、指标、模型和窗口展开，输出 `rmse`、`mae`、`mape`、`p95_error` 等指标。`rows` 提供扁平记录，`resources` 提供按资源聚合的嵌套结构，便于报表、审计和模型效果对比。

### `generation_stats.json`

本次预测的统计信息：资源数、预测模型、窗口参数、耗时、输出大小、误差报告文件名等。

### `backups/`

增量合并前自动创建的 `raw_data.json` 时间戳备份，格式为 `raw_data.YYYYMMDD-HHMMSS.json`。
