# 统一运行配置精简设计

## 目标

精简 `resource_predict/settings.py` 中数量过多、面向实现细节的配置项，将保留的业务运行配置全部迁移到 Web 页面统一管理。保存后立即影响后续预测、决策和 K8S Prometheus 拉取；调度设置变化时安全重载后台调度器。

系统启动前必须确定的监听、目录和日志设置不进入页面，继续作为启动常量保留。

## 配置边界

### 启动常量

`settings.py` 仅保留以下启动期设置，代码命名改为 `bootstrap_settings`，避免与运行配置混淆：

- Flask 静态目录和模板目录；
- 输出目录；
- 日志文件、级别和控制台输出；
- 监听 host、port 和 debug。

这些值在页面可用前已经生效，不支持运行时修改。

### 页面运行配置白名单

运行配置保存到 `deploy/runtime_config.json`，仅允许以下字段。

#### 数据采集 `collection`

| 字段 | 默认值 | 页面含义 |
| --- | --- | --- |
| `scheduled_update_enabled` | `true` | 启用 K8S Prometheus 定时拉取 |
| `scheduled_update_interval_minutes` | `360` | 自动拉取周期（分钟） |
| `history_days` | `7` | 首次或全量拉取历史天数 |
| `step_seconds` | `300` | Prometheus range query 步长 |
| `rate_window` | `5m` | CPU `rate()` 窗口 |
| `request_timeout_seconds` | `300` | 单次 Prometheus 请求超时 |

`incremental_overlap_minutes=60`、`scheduled_update_startup_delay_seconds=60` 和 `fail_fast=false` 变为 K8S 接入模块内部常量。全局 namespace 过滤删除，继续使用页面中已有的单集群 `namespace_regex`。

#### 预测 `prediction`

| 字段 | 默认值 | 页面含义 |
| --- | --- | --- |
| `vm_test_duration` | `72h` | VM 模型验证窗口 |
| `vm_future_duration` | `24h` | VM 未来预测窗口 |
| `workload_test_duration` | `24h` | K8S Workload 模型验证窗口 |
| `workload_future_duration` | `24h` | K8S Workload 未来预测窗口 |
| `enabled_methods` | `seasonal_naive, prophet` | 参与候选选择的模型 |
| `enable_ensemble` | `false` | 是否加入 Ensemble 候选 |

Prophet 超参数、裁剪参数、路由参数、异常阈值、回测折数、模型复用开关和并发数变为对应预测模块的内部常量。mock 数据规模、随机种子和生成点数不再属于生产运行配置，由 mock CLI 或 provider 自身默认值负责。

#### 决策 `decision`

| 字段 | 默认值 | 页面含义 |
| --- | --- | --- |
| `default_policy_tier` | `balanced` | 默认策略：保守、均衡或激进 |
| `scale_out_threshold` | `0.8` | 扩容使用率阈值 |
| `scale_in_threshold` | `0.2` | 缩容使用率阈值 |
| `scale_in_max_reduction_ratio` | `0.5` | 单次最大缩容比例 |
| `scale_out_confirmations` | `2` | 扩容确认轮次 |
| `scale_in_confirmations` | `3` | 缩容确认轮次 |
| `scale_out_cooldown_minutes` | `60` | 扩容冷却时间 |
| `scale_in_cooldown_minutes` | `360` | 缩容冷却时间 |
| `conservative_namespaces` | `prod, production, payments, core, platform` | 自动采用保守策略的命名空间 |
| `aggressive_namespaces` | `dev, test, staging, batch` | 自动采用激进策略的命名空间 |

P95 保护、趋势斜率、峰谷差、目标利用率、最小规格、CPU 对齐和状态保留天数等算法细节转为决策模块内部常量。

### 集群接入

现有 VM/K8S 调配集群和 Prometheus 集群字段继续由页面管理，存储文件仍为：

- `deploy/clusters.json`；
- `deploy/k8s_prometheus_clusters.json`。

这两类配置包含结构不同的凭据和控制节点信息，不并入 `runtime_config.json`。页面和 API 统一，但专业存储边界保持不变。

## 后端架构

### 启动设置

`resource_predict/settings.py` 只定义启动设置。原来从 `settings.app` 读取的代码改为读取 `bootstrap_settings`。分页、缓存、输出分片和其他实现参数移动到消费它们的模块，使用有名称的内部常量。

### 运行配置存储

新增运行配置服务，职责为：

1. 定义 `CollectionConfig`、`PredictionConfig`、`DecisionConfig` 和完整不可变快照；
2. 从 `deploy/runtime_config.json` 读取和严格校验；
3. 通过锁原子替换当前内存快照；
4. 使用同目录临时文件和 `os.replace()` 原子保存 UTF-8 JSON；
5. 暴露 `get_runtime_config()`，让一次预测、决策或拉取在开始时取得一致快照。

未知字段直接拒绝，防止拼写错误静默无效。数值字段执行类型、正数、比例区间和相互关系校验。配置文件不存在时使用默认快照；JSON 损坏或校验失败时使用默认快照，同时把错误返回页面。

业务代码不长期缓存分区对象。每个新任务在入口取得快照并显式向下传递，使同一个任务执行过程中不会混用保存前后的值。

### 统一配置 API

新增：

- `GET /api/system-config`：返回运行配置、VM/K8S 调配集群、Prometheus 集群、支持的模型和加载告警；
- `PUT /api/system-config`：统一校验完整 payload，全部通过后才保存并切换内存快照。

错误响应包含稳定的 `field` 路径，例如 `runtime.collection.step_seconds`，页面据此定位字段。保存流程先规范化全部分区，再生成所有目标文件内容；写文件失败时保留旧内存快照并恢复本次已替换文件，避免页面显示成功但只有部分配置生效。

原 `/api/cluster-configs` 和 `/api/forecast-config` 页面调用迁移到统一接口。与诊断、手动拉取有关的动作 API 保留。

## 调度器重载

K8S 调度器保留单一后台线程。配置保存后发出重载事件，调度循环醒来并读取新快照：

- 关闭定时拉取时进入等待状态，不销毁并立即重建竞争线程；
- 开启或修改周期时重新计算下一次等待；
- 正在执行的拉取不中断，完成后使用新配置；
- 任何时刻最多存在一个自动调度线程。

这满足运行时立即接受新设置，同时避免 stop 超时后旧、新线程并存。

## 页面设计

导航中的“集群配置”改为“系统配置”。页面从上到下分为：

1. 数据采集；
2. 预测配置；
3. 扩缩容策略；
4. 集群接入。

所有保留字段直接显示，不提供“高级配置”。策略等级和模型使用明确的 select/checkbox；比例字段在页面显示为百分比、提交时规范化为 `0..1`；时长和单位写在标签中。命名空间使用逗号分隔输入并规范化为字符串数组。

页面只有一个“保存全部配置”按钮。保存中禁用按钮；成功后重新渲染服务端规范化结果；失败时在顶部显示摘要并在对应字段旁显示错误，不清空用户输入。

## 旧配置迁移

当 `runtime_config.json` 不存在时，运行配置服务使用新默认值，并从现有 `deploy/forecast_config.json` 读取仍在白名单内的 `enabled_methods` 和 `enable_ensemble`。首次成功保存系统配置后创建 `runtime_config.json`，此后不再读取旧预测配置。

仓库中的 `deploy/forecast_config.json` 不再作为主配置文件，相关页面、API、README 和 `docs/` 引用迁移到 `runtime_config.json`。集群配置文件不删除、不改名、不丢失凭据。

## 错误处理与安全

- 所有 JSON 文件继续以 UTF-8 写入；
- 写入使用同目录临时文件和原子替换；
- 密码/Token 不写日志或错误消息；
- API 不接受页面白名单以外的运行配置字段；
- 配置保存失败不改变内存快照或调度行为；
- 启动时配置损坏不会阻止页面启动，页面显示加载告警并允许用有效配置覆盖修复。

## 测试与验收

### 后端

- 默认快照、旧预测配置迁移和损坏文件回退；
- 白名单、未知字段、类型、范围和跨字段校验；
- 原子保存、失败回滚和线程安全快照替换；
- 预测、决策和 K8S provider 从任务快照读取新值；
- 调度器启用、禁用、改周期和执行中重载时保持单线程；
- 统一 API 的读取、保存、字段错误和整体失败不生效。

### 前端

- 四个配置区域渲染和 payload 收集；
- 百分比、时长、命名空间和模型字段规范化；
- 服务端字段错误定位和保存失败保留输入；
- 现有新增/删除集群、诊断和手动拉取行为不回归。

### 回归与页面检查

- Node JavaScript 测试；
- compileall、pyflakes、vulture 和完整 pytest；
- 浏览器检查桌面和窄屏布局、读取、编辑、校验失败、保存成功及调度提示更新。

## 非目标

- 不支持在页面修改 host、port、目录或日志启动设置；
- 不提供任意键值或 JSON 高级编辑器；
- 不修改预测算法、决策公式或 Prometheus 指标定义；
- 不把生产凭据合并进 `runtime_config.json`；
- 不为多 Web worker 提供分布式调度器。

