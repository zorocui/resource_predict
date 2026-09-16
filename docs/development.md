# 开发指南与 FAQ

本文档包含测试策略、开发约定和常见问题排查。

## 连续数据不足的预测日志

出现“跳过最近连续段有效点数不足的资源”时，查看同轮的“指标预测跳过”日志。日志逐项列出资源 ID、指标（容器指标带 `container/容器名/` 前缀）、总有效点数、最近连续段起止时间与点数、最低需要的 `test_size + 1` 点，以及还差多少点。

如果连续段被缺口截断，还会列出最后一次截断前后的有效点时间、缺失采样点数、缺失时刻范围和采样间隔。范围包含首尾缺失点，按采样间隔即可定位每个缺失时刻；所有时间使用带 `+00:00` 的 UTC，换算北京时间需加 8 小时。日志描述的是合并、补齐后进入预测的数据，不能据此统计采集端所有原始缺失。没有截断缺口时明确说明历史长度不足，不推测不存在的缺失时刻。“距最低要求还差”与“缺口缺失点数”是不同含义。此日志增强不改变预测门槛或跳过策略。

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
| `test_scaling_history.py` | 全局记录分页、搜索、历史规格冻结与完成时间 |
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

## 导出单个 Workload 的准确率诊断数据

Workload 详情页的“重新拉取预测”会查询目标集群、命名空间的历史归属，将 CPU、内存、容量查询限制到该 Workload 的新旧 Pod，拉取集群配置的完整历史窗口，再仅合并与重算当前资源。重复点击或已有更新任务时拒绝并发执行；数据没有新时间点也会强制重新预测。缺口无法补回、最新连续段仍不足时，预测仍会按原有质量规则跳过，不会伪造连续数据。任务状态与记录沿用“数据更新”，完成后刷新队列及当前详情。

单资源强制重算使用 `local_update=True` 局部产物发布：只读取目标详情分片，写入以资源 ID 哈希命名的独立详情文件并切换摘要引用，其他详情和全量 manifest 不读不写。重复重算复用同一独立文件名，避免每次积累新文件。摘要、准确率和误差汇总仍需读写各自的 JSON 文件，但保留其他资源记录；准确率页面提示包含不同预测批次的最新记录。raw 旧分片清理延后到后台。单资源任务在结束时独立写入根输出目录 `update_history.json`，包含资源 ID、完整起止时间、成功/失败结果，不使用共享任务状态的去重时间作为记录依据。

调配后出现额外断档时，检查 `[k8s_prometheus] 历史归属恢复` 日志。采集使用同一查询窗口的 Pod/ReplicaSet 历史归属恢复已退出副本，历史 Request/Limit 按时间点与使用量配对；当前规格仍使用即时查询。同名对象在窗口内有归属冲突时排除，不猜测。Prometheus 未保留历史归属时会告警，无法保证恢复已退出副本；当前有资源配置但历史容量查询无结果时，本轮集群采集失败并保留本地旧数据，避免用新规格覆盖旧利用率。历史容量查询可用但部分采样缺失时保留缺口，不把缺少副本的使用量当作总量。

升级本身不补回旧 raw 缺口；需重新拉取覆盖缺口的时间范围（必要时全量拉取）。历史源数据仍可查询时才能修复，不应通过插值跨越大缺口。

在项目根目录运行（脚本仅依赖 Python 标准库，可单独复制到服务器）：

```bash
python tools/export_workload_diagnostics.py
# 或直接指定名称、产物目录
python tools/export_workload_diagnostics.py my-workload --out-dir outputs/k8s --dest diagnostics
```

省略名称时交互输入；重名时列出完整资源 ID，使用完整 ID 重试。ZIP 只包含选中资源的摘要、预测详情、raw、raw 索引引用、准确率汇总和导出说明，不读取全量 manifest 或集群连接配置，不触发采集/预测。缺失准确率汇总会提示；源文件在导出期间变化会中止。原始观测与准确率独立测试可能来自不同次运行或预处理流程，包内保留时间及校验信息，不能仅凭导出时间认定它们属于同次预测。复制脚本到项目根目录时，命令改为 `python export_workload_diagnostics.py`。

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
- 概览列出全部支持的模型（含LSTM），最优次数为0也显示，未启用模型明确标注“未启用”，同时保留旧产物里仍出现的其他模型。新摘要保存 `container_best_methods`；Workload 优先统计各容器各指标的最优模型次数，不重复统计 Workload 汇总。旧摘要缺少该字段时退回汇总指标计数，重新预测后补齐容器统计。
- 风险队列根据 `spec.last_scaled_at_epoch_ms` 显示“已调配”标记，悬停显示北京时间的最近成功调配时间。预检、失败任务不会设置该时间；标记不随冷却期结束消失，也不代表当前建议可以忽略。缺少历史时间的旧资源不会推断为已调配。
- “调配记录”页展示全部资源已保留的任务（包括预检，任务文件仍最多1000条），不依赖风险队列选中资源。新任务创建时冻结 `before_spec`，终态记录 `finished_at_ms`；历史字段缺失显示“未记录”。`GET /api/scaling-history` 返回分页 `items/total/page/page_size`，只传规格、时间、结果等展示字段，不传 SSH 命令和输出。失败、预检和执行中的记录显示目标规格，成功后的规格仅代表命令结果，监控确认仍以调配成效为准。
- 调配快照写入 raw 时使用 `defer_cleanup=True`，不在调配线程扫描或删除旧分片。同目录请求合并，距最后一次提交约300秒后由后台守护线程清理；常规采集提交仍可回收过期分片。扫描不持有写锁，删除前在锁内核对最新索引，保留当前引用和300秒宽限期内的文件。进程退出可中止待清理任务，后续数据提交继续回收，不影响已提交数据。
- 调配快照仅回写详情分片、摘要和目标资源 raw 数据，使用 `ingest_scaling_evidence=False`，不重复处理旧采集证据；常规采集仍默认更新成效证据。调配不读取或重写 `manifest.json`，返回的 `manifest_updated` 为 `false`。manifest 保留预测生成时的规格与建议，由下一次预测产物写出时更新，不代表实时规格；实时规格应通过资源 API 获取。排查“正在同步本地快照”时，检查 `[scaling] snapshot stage` 日志中的 `waiting_lock`、`detail`、`summary`、`raw` 及各阶段 `elapsed_seconds`。

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
python -m pytest -q tests/test_forecast_evaluation.py tests/test_forecast_optimizations.py tests/test_forecasting.py tests/test_forecast_error_report.py
```

留档只保存预测与当时规格，不代表已经完成生产收益评估。必须后续对齐真实观测并进行回放/受控执行，才能报告容量不足、预留量或服务质量收益。万级资源测试按容器与指标展开后的序列数衡量；图表历史点数上限不等于模型训练点数上限。

独立测试的输入契约是按时间可获得的序列。K8S 新采集短缺口只向前填补；已有 raw 缓存可能来自旧版双向插值，不能逆向恢复为原始观测，严格实验应重新采集或使用未插值输入。自定义 Provider 必须保证清洗、规格归一化与特征构造不使用预测起点之后的信息。填补值仍属于加工数据，生产评分应以后续真实采样点为准。

### 轻量准确率验证

逐点留档与SQLite导入已删除。运行 `python -m pytest -q tests/test_accuracy_summary.py tests/test_multicore_pipeline.py`，验证自动生成汇总、选用模型、5个百分点与实际值5%取较大容差的边界、高使用率/零值、旧口径排除、缺少基线排除、容器去重、无效值、无SQLite/留档副作用及多核输出。前端运行 `node --test tests/js/test_accuracy_summary.mjs`。


## 执行安全约定

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
