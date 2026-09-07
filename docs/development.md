# 开发指南与 FAQ

本文档包含测试策略、开发约定和常见问题排查。

## 测试文件

| 测试文件 | 覆盖范围 |
| --- | --- |
| `test_forecasting.py` | ARIMA / SARIMA / Prophet / Naive / Rolling 预测方法 |
| `test_forecast_windowing.py` | 预测窗口解析、频率推断、时长换算 |
| `test_decision.py` | VM 扩缩容判断、目标规格计算、置信度评分、风险画像 |
| `test_k8s_workload_decision.py` | K8S 决策、副本数建议、数据质量处理 |
| `test_io.py` / `test_raw_store.py` | 时间戳解析、raw 资源分片、索引原子替换与完整性校验 |
| `test_scaling_executor.py` | 调配计划构建、flavor 选择、命令生成 |
| `test_scaling_api.py` | 调配 API 端点 |
| `test_scaling_tasks.py` | 任务生命周期管理 |
| `test_scaling_security.py` | 命令注入防护、安全校验 |
| `test_output_health.py` | 产物健康检查逻辑 |
| `test_output_isolation.py` | VM / K8S 产物隔离 |
| `test_cluster_configs.py` | 集群配置读写 |
| `test_runtime_config.py` | 统一运行配置白名单、字段校验、旧预测配置迁移与损坏文件兜底 |
| `test_system_config.py` | 采集可靠性参数校验、页面视图分离、聚合保存与失败时整体不写入 |
| `test_forecast_config.py` | 预测模型开关归一化与非法取值校验 |
| `test_k8s_workload_provider.py` | K8S Prometheus 数据聚合 |
| `test_k8s_scheduler_reload.py` | 保存配置只唤醒调度线程重读配置，不额外触发拉取 |
| `test_utils.py` | 公共工具函数 |

## 运行测试

```bash
# 全部测试
python -m pytest -q

# 单个文件
python -m pytest tests/test_forecasting.py -q

# 单个用例
python -m pytest tests/test_forecasting.py::test_function_name -q
```

## 回归检查

每次修改后按顺序运行以下四项检查：

```bash
# 1. 编译检查（语法错误）
python -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict benchmarks tests

# 2. 静态分析（未使用导入等）
python -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict benchmarks tests

# 3. 死代码检测
vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict benchmarks tests --min-confidence 80

# 4. 测试
python -m pytest -q
```

## 预测反馈账本性能基准

```bash
python -m benchmarks.forecast_feedback_benchmark --resources 10000 --containers 1 --batches 7 --points 24 --result outputs/feedback_benchmark_result.json
```

默认在临时目录构造 1 万资源、每资源 1 个容器、每容器 4 指标、7 个每日批次和每曲线 24 个点，共 672 万点。最后一天 96 万点用于补评；测量评分、报告和 4 万条指标校准，输出进程峰值 RSS、读写计数（Windows）或块写计数（Linux）、数据库体积和结果摘要。不访问生产监控或生成生产扩缩容任务。

可用 `--database outputs/feedback_benchmark.sqlite3` 保留合成库复测，现存库会检查批次命名、资源标识和规模，不符合时拒绝修改。`--containers`、`--batches`、`--points` 可扩大负载；务必按实际预测频率选择批次规模。数据不包含完整影子配对/上界留档，不计模型拟合、raw 序列化、Prometheus 拉取、真实磁盘竞争或持续到期清理的吞吐，不能直接作生产 SLA。

2026-09-06 本机单进程重复负载测量：旧实现报告 23.4–26.5 秒，优化后 12.3–13.2 秒；校准旧实现 39.3–44.7 秒，优化后 33.6 秒。补评约 6–7 秒基本不变。优化版峰值 RSS 67.0 MiB，旧版复测 63.9 MiB；复用库约 590 MiB，文件空闲页复用，不承诺每轮收缩。Windows 进程累计写入约 2.73 GiB 降至 1.28 GiB，包含合成重置、SQLite 日志及临时排序文件，不能等同于物理磁盘写入。该测量不是多次统计置信区间。

校准通过单次有序游标选取相同样本，每组最多保留 500 个；报告先汇总曲线/提前量，再按模型与单位汇总，并保持按实际点数加权。浮点累加顺序变化可能产生末位差异。raw 提交延迟报告，预测结束统一发布，减少一次完整汇总。回归命令：`python -m pytest -q tests/test_feedback_performance.py tests/test_calibration.py tests/test_realized_error.py`。

## 详情加载性能基准

该基准生成大规模资源分片但跳过模型拟合，测量单资源元数据与单指标图表读取的 P50/P95：

```bash
python -m benchmarks.resource_detail_benchmark --resources 1000 --points 2016 --samples 50
```

默认验收阈值为元数据 P95 不高于 200ms、图表 P95 不高于 500ms，并校验训练历史响应不超过 1000 点。

## 代码组织约定

- 根目录只放直接运行的 CLI 或项目级配置文件
- 所有业务逻辑放入 `resource_predict/` 包内，CLI 只做参数解析和输出
- 新增 K8S 相关代码使用 `workload` 命名；`pod` 仅作为 Prometheus / Kubernetes 标签或观测字段
- 预测产物统一称为 `outputs` 或 `forecast artifacts`，不使用 `images` 命名
- 注释和日志消息使用中文

## 配置约定

- 所有配置 dataclass 使用 `frozen=True`，通过替换而非赋值来修改
- 时间戳：API/payload 层使用毫秒级 Unix int；内部使用 pandas `DatetimeIndex`
- `predict_only=True` 模式绝不修改 `raw_index.json` 或 `raw/` 资源分片
- 不提交 `outputs/`、日志、缓存、`__pycache__`、本地凭据文件

## 资源类型系统

| 规范名 | 来源字符串 | 指标集 |
| --- | --- | --- |
| `openstack_vm` | `openstack`, `vm`, `openstack_vm` | `cpu`, `memory`, `disk` |
| `k8s_workload` | `k8s_workload`, `workload`, `controller`, `k8s`, `kubernetes` | `cpu_limit`, `cpu_request`, `memory_limit`, `memory_request` |

使用 `resource_type_of(item)` 归一化类型，使用 `metric_names_for_resource(item)` 获取指标名列表。历史资源类型输入（如 `pod`、`k8s_pod`、`container`、`k8s_container`）不再兼容；开发阶段数据可重新导入为 `k8s_workload`。

## Provider 接口

所有数据源必须返回统一结构：

```python
{
    "resource_id": str,
    "resource_type": "openstack_vm" | "k8s_workload",
    "spec": {"cluster": str, "instance_id": str, ...},
    "metrics": {
        "cpu":    {"timestamps": [int_ms, ...], "values": [float_0_to_1, ...]},
        "memory": {"timestamps": [...], "values": [...]},
        # "disk" for VM only
    }
}
```

增量 Provider 签名为：

```python
(prepared_resources: List[Dict], points_to_add: int) -> List[Dict]
```

## 可信预测基线的验证

第一阶段设计见 [预测评估设计](superpowers/specs/2026-09-05-forecast-evaluation-design.md)，实施记录见 [实施计划](superpowers/plans/2026-09-05-forecast-evaluation.md)。

使用 `python generate_forecasts.py predict` 可在现有数据上重新生成独立测试误差报告与预测留档。科研比较应固定输入快照、候选模型、窗口与运行版本；误差报告的基础指标是独立测试误差，选型用的是另行记录的训练段内部验证误差。多个滚动验证折用于选型，不应冒充多个独立测试实验。需要多个独立预测起点时，应逐次截断输入数据重复运行，不能先读取未来数据再裁剪结果。

定向回归覆盖测试标签不影响选型或权重、最新观测影响未来曲线、集成真实误差、短历史降级、失败模型身份、分钟级日周期和留档发布/保留：

```bash
python -m pytest -q tests/test_forecast_evaluation.py tests/test_forecast_optimizations.py tests/test_forecasting.py tests/test_forecast_archive.py tests/test_forecast_error_report.py
```

留档只保存预测与当时规格，不代表已经完成生产收益评估。必须后续对齐真实观测并进行回放/受控执行，才能报告容量不足、预留量或服务质量收益。万级资源测试按容器与指标展开后的序列数衡量；图表历史点数上限不等于模型训练点数上限。

独立测试的输入契约是按时间可获得的序列。K8S 新采集短缺口只向前填补；已有 raw 缓存可能来自旧版双向插值，不能逆向恢复为原始观测，严格实验应重新采集或使用未插值输入。自定义 Provider 必须保证清洗、规格归一化与特征构造不使用预测起点之后的信息。填补值仍属于加工数据，生产评分应以后续真实采样点为准。

### 真实误差回填验证

`tests/test_realized_error.py` 覆盖重复评分、迟到补评、原曲线保留、精确时间、未来观测排除、规格变化、容器/scope 隔离、非有限值、导入回滚、保留期和 raw/upsert 自动链路。provider 测试验证采集证据不包含填补点。

```bash
python -m pytest -q tests/test_realized_error.py tests/test_k8s_workload_provider.py tests/test_forecast_archive.py
python -m resource_predict.pipeline.realized_error --out-dir outputs/k8s
```

逐点追溯可使用 SQLite 查询（`container=''` 表示资源聚合指标）：

```sql
SELECT c.batch, c.resource_id, c.container, c.metric, c.model, c.unit,
       p.target_ms, p.predicted, p.actual, p.scored_at_ms, p.observation_source,
       p.target_ms-c.data_end_ms AS data_horizon_ms,
       p.target_ms-c.issued_ms AS publication_horizon_ms, p.skip_reason
FROM points p JOIN curves c ON c.id=p.curve_id;
```

原始证据仅保存最新接收批次，首次真实评分固定；监控修订不覆盖既有评分。生产性能应测量实际容器数、预测频率、窗口点数下的数据库大小和评分耗时。不以 mock 精度证明生产收益；基于真实残差的经验上界见下节。

### 经验上界验证

```bash
python -m pytest -q tests/test_calibration.py tests/test_realized_error.py tests/test_io.py
```

校准测试验证历史已评分样本的时间隔离、同目标去重、配置/版本/模型/容器/规格隔离、分提前量缺样本降级、负残差余量下限、只读账本异常、留档覆盖评分、增量合并和 API 输出。新增表通过 CREATE TABLE IF NOT EXISTS 增量创建，不重写旧预测或评分。

覆盖率必须用校准后真正到达的数据测量，不能拿训练余量的同一批残差证明效果；均值余量也不等同于资源费用。保留 7 天、单资源严格分组可能导致样本不足，默认保留原建议。生产验证应报告样本数、时段、业务漂移、覆盖率及运行耗时。

## 执行安全约定

页面校准反馈验证：`python -m pytest -q tests/test_forecast_feedback.py`；前端状态与转义检查：`node tests/feedback_ui_check.cjs`。详情反馈接口按报告 mtime 缓存解析结果与资源索引，只返回当前资源；页面缺值使用破折号、报告陈旧/已过期单独提示，不将旧产物当作有效新评审。

受控采用回归：`python -m pytest -q tests/test_controlled_activation.py tests/test_action_gate_state.py tests/test_scaling_tasks.py`。测试覆盖默认关闭、显式资源列表、完整建议重建、原建议保留/回退、非新预测、过期或错误报告、规格/配置/目标变化、策略切换确认重置、execute 入队及排队后失效拒绝，以及新评分推翻旧报告。所有测试使用本地夹具/替身，不执行真实远程命令。

受控采用仅在生成正式建议时允许切换；文件/数据库异常拒绝校准授权。旧报告期限尚未到达也不代表当前账本仍通过判定，执行入口按具体资源重新评审。两个代码级开关默认 False/空列表，本次测试不修改生产配置。

启用判定回归：`python -m pytest -q tests/test_activation_assessment.py`。测试涵盖只读性、满足条件、风险或超出幅度增加、无预留收益、完整率门槛、未来评分、陈旧预测/观测、连续证据重置、变化增多、去重先于观测筛选、资源/容器隔离、K8S limit 不算预留收益、缺基准和报告入口幂等。`eligible_for_review` 不启用策略；当前规则是代码级工程判据，不是覆盖率置信区间或生产 SLA。

2026-09-07 合成判定负载：1000 个 K8S 资源 × 2 容器 × 4 指标 × 16 轮 × 12 点 = 1536000 点，判定约 5.56 秒，数据库约 160 MB。使用测试夹具构造满足规则的数据，仅测判定模块，不代表生产资源已具备启用条件；原始结果为 outputs/activation_benchmark.json。

影子对照测试位于 `tests/test_shadow.py`：验证原建议不变、同算法换输入、缺校准/缺容器不配对、VM 数值、容器 request/limit 与小规格、副本变化不伪评分、留档幂等、变更率、保留期和部分重算。可运行 `python -m pytest -q tests/test_shadow.py`。`shadow_comparison.executable=false`，快照不包含执行 gate，不能作为 execute 请求的授权来源。

生产观察应同时检查配对覆盖数、未评估原因、同一批次两方案的资源量/超出率和变更次数。资源量均值按建议曲线计算，风险比例按匹配真实点计算，二者分母不同；不得把监控缺失当作风险为零。

- 调配命令中所有用户可控值使用 `shlex.quote()` 转义
- 不拼接未转义的字符串构建 shell 命令
- DaemonSet 副本缩放显式跳过并给出警告
- 磁盘缩容限制最小 50GB

---

## 附录：常见问题

| 问题 | 处理 |
| --- | --- |
| 页面无数据 | 先运行 `python generate_forecasts.py`，再运行 `python check_outputs.py` 检查 |
| VM 有数据，K8S 为空 | 检查 Prometheus 配置，运行 `python ingest_k8s_workloads.py --diagnose` |
| 提示缺少 K8S Prometheus 配置 | 设置 `K8S_PROMETHEUS_CLUSTERS` 环境变量或写入 `deploy/k8s_prometheus_clusters.json` |
| VM 调配提示缺少配置 | 检查 `deploy/clusters.json` 中是否存在与 `spec.cluster` 同名的 OpenStack 集群 |
| K8S 调配提示缺少配置 | 检查 `deploy/clusters.json` 中是否存在与 `spec.cluster` 同名且 `cloud_type=k8s` 的集群 |
| OpenStack flavor 发现失败 | 确认控制节点可 SSH 登录，且 `openstack_rc` 加载后可执行 `openstack flavor list -f json` |
| 产物结构不一致 | 运行 `python check_outputs.py --json` 查看具体错误 |
| 测试工具缺失 | 运行 `python -m pip install -r requirements-dev.txt` |
| 更新任务冲突（409） | 查询 `/api/update-status` 确认当前是否有更新在执行中，等待完成后重试 |
| 预测模型未生效 | 在 Web 的"系统配置"页面 → "预测配置"分区修改候选模型后保存，再触发重新预测；保存只影响新任务，已有产物需要重新生成 |
| 资源详情返回 202 | 资源正在等待预测完成，稍后重试即可 |
