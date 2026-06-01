# 开发指南与 FAQ

本文档包含测试策略、开发约定和常见问题排查。

## 测试文件

| 测试文件 | 覆盖范围 |
| --- | --- |
| `test_forecasting.py` | ARIMA / SARIMA / Prophet / Naive / Rolling 预测方法 |
| `test_forecast_windowing.py` | 预测窗口解析、频率推断、时长换算 |
| `test_decision.py` | VM 扩缩容判断、目标规格计算、置信度评分、风险画像 |
| `test_k8s_workload_decision.py` | K8S 决策、副本数建议、数据质量处理 |
| `test_io.py` | raw_data.json 读写、时间戳解析、混合格式 |
| `test_scaling_executor.py` | 调配计划构建、flavor 选择、命令生成 |
| `test_scaling_api.py` | 调配 API 端点 |
| `test_scaling_tasks.py` | 任务生命周期管理 |
| `test_scaling_security.py` | 命令注入防护、安全校验 |
| `test_output_health.py` | 产物健康检查逻辑 |
| `test_output_isolation.py` | VM / K8S 产物隔离 |
| `test_cluster_configs.py` | 集群配置读写 |
| `test_forecast_config.py` | 预测模型配置读写 |
| `test_k8s_workload_provider.py` | K8S Prometheus 数据聚合 |
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
python -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests

# 2. 静态分析（未使用导入等）
python -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests

# 3. 死代码检测
vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80

# 4. 测试
python -m pytest -q
```

## 代码组织约定

- 根目录只放直接运行的 CLI 或项目级配置文件
- 所有业务逻辑放入 `resource_predict/` 包内，CLI 只做参数解析和输出
- 新增 K8S 相关代码使用 `workload` 命名；`pod` 仅作为 Prometheus 标签或历史产物兼容词
- 预测产物统一称为 `outputs` 或 `forecast artifacts`，不使用 `images` 命名
- 注释和日志消息使用中文

## 配置约定

- 所有配置 dataclass 使用 `frozen=True`，通过替换而非赋值来修改
- 时间戳：API/payload 层使用毫秒级 Unix int；内部使用 pandas `DatetimeIndex`
- `predict_only=True` 模式绝不修改 `raw_data.json`
- 不提交 `outputs/`、日志、缓存、`__pycache__`、本地凭据文件

## 资源类型系统

| 规范名 | 来源字符串 | 指标集 |
| --- | --- | --- |
| `openstack_vm` | `openstack`, `vm`, `openstack_vm` | `cpu`, `memory`, `disk` |
| `k8s_workload` | `k8s_workload`, `workload`, `controller`, `k8s_controller`, `pod`, `k8s_pod`, `k8s`, `kubernetes`, `container`, `k8s_container` | `cpu_limit`, `cpu_request`, `memory_limit`, `memory_request` |

使用 `resource_type_of(item)` 归一化类型，使用 `metric_names_for_resource(item)` 获取指标名列表。所有 K8S 相关字符串统一归一到 `k8s_workload`。

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

## 安全约定

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
| 预测模型未生效 | 在 Web 页面"预测模型"中修改后保存，再触发重新预测 |
| 资源详情返回 202 | 资源正在等待预测完成，稍后重试即可 |
