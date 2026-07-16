# K8S 更新历史逐集群结果设计

## 背景与目标

当前 K8S Prometheus 拉取支持一次处理多个集群，但更新状态和 `outputs/update_history.json` 只记录任务来源、拉取窗口和全局资源统计。Provider 虽然在日志中记录单个集群的成功或异常，上层却只接收合并后的 Workload 列表，因此页面无法说明本次拉取涉及哪些集群，也无法表达多集群部分成功。

本次改造要求每条 K8S Prometheus 更新记录都保存并展示每个目标集群的成功或失败情况。多个集群中既有成功又有失败时，整体状态使用 `partial_success`。成功集群的数据继续进入 raw upsert 和预测流程，失败集群不得阻断成功数据；非 K8S 更新记录保持现有行为。

## 范围

本次包含：

- K8S Prometheus Provider 输出逐集群拉取结果。
- 更新运行状态和持久化历史保存逐集群结果。
- 更新历史 API 返回逐集群结果。
- “历史更新记录”页面展示逐集群状态、资源数、耗时和错误。
- 全成功、部分成功、全部失败及旧记录兼容测试。
- 更新 API 文档和架构文档中的更新历史字段说明。

本次不恢复 `app.py` 自动调度线程，不改变六小时拉取任务由何种外部机制触发，不修改 Prometheus 查询语句、预测模型或扩缩容决策。

## 方案选择

采用 Provider 返回结构化结果、保留现有列表接口兼容性的方案。

新增一个结构化拉取入口，返回：

```python
{
    "items": [...],
    "cluster_results": [
        {
            "cluster": "cluster-a",
            "status": "success",
            "resources_fetched": 32,
            "elapsed_seconds": 12.4,
            "error": None,
        },
        {
            "cluster": "cluster-b",
            "status": "failed",
            "resources_fetched": 0,
            "elapsed_seconds": 3.1,
            "error": "Prometheus timeout",
        },
    ],
}
```

现有 `k8s_workload_prometheus_provider()` 继续返回 `List[Dict[str, Any]]`，内部调用结构化入口并只取 `items`，避免破坏 CLI、诊断、测试和通用 Provider 接口。`services/k8s_ingest.py` 改用结构化入口，从而取得逐集群结果并贯穿状态与历史记录。

不采用 Service 层逐集群重复调用 Provider，因为这会重复解析配置、改变资源上限语义并增加网络流程编排。也不根据 `resource_id` 推断成功集群，因为该方式无法区分拉取失败与成功但无数据。

## 逐集群结果语义

`cluster_results` 中每个元素包含固定字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cluster` | string | 配置或请求中的集群标识 |
| `status` | string | 仅允许 `success` 或 `failed` |
| `resources_fetched` | integer | Provider 为该集群生成的 Workload 数，最小为 0 |
| `elapsed_seconds` | number/null | 该集群拉取耗时，保留两位小数 |
| `error` | string/null | 失败原因；成功时为 null |

判定规则：

- `_fetch_target()` 正常完成且至少返回一个可聚合 Workload：该集群为 `success`。
- Prometheus 请求、响应解析或 Workload 聚合抛出异常：该集群为 `failed`，保存经过字符串化的异常信息。
- `_fetch_target()` 正常完成但返回零个可聚合 Workload：该集群为 `failed`，错误固定为“Prometheus 未返回可聚合的 K8S Workload”。这与当前上层拒绝空拉取结果的行为一致。
- 请求中包含未配置的集群名称：该名称也必须形成一条 `failed` 结果，错误说明未找到集群配置；已配置集群仍继续拉取。
- `fail_fast=True` 时，第一个运行时失败后不再发起其余集群请求；未执行的目标仍生成 `failed` 结果，错误说明“因 fail_fast 在前序集群失败后未执行”，从而保证每个目标集群都有明确记录。
- 列表顺序按实际目标配置顺序；请求中未配置的名称追加在列表末尾并按名称排序，保证展示稳定。

认证信息和 Prometheus URL 不进入 `cluster_results`，避免更新历史泄露凭据或内部地址。

## 整体状态与数据流

K8S 拉取数据流调整为：

```text
API/外部任务
  -> run_k8s_prometheus_upsert
  -> 结构化 Provider 拉取
  -> items + cluster_results
  -> 成功 items 执行 run_upsert_with_data
  -> mark_external_update_finished / mark_external_update_failed
  -> outputs/update_history.json
  -> GET /api/update-history
  -> 历史更新记录页面
```

整体状态按完整更新任务判定：

- 所有目标集群成功，且后续 upsert/预测成功：`success`。
- 至少一个集群成功、至少一个集群失败，且成功集群数据的 upsert/预测成功：`partial_success`。
- 所有目标集群失败：`failed`，不调用 upsert/预测。
- Provider 拉取后，upsert、raw 写入或预测流程失败：整体为 `failed`；逐集群结果仍保留其拉取阶段的真实状态。
- 配置解析等无法形成任何目标集群的全局错误：整体为 `failed`，`cluster_results` 可为空；错误继续写入顶层 `error`。

`mark_external_update_started()` 的 metadata 支持 `cluster_results`，初始可为空。Provider 返回后，在终态写入之前更新状态中的完整结果。`_record_current_update_history()` 将其复制到历史记录。状态对象的可变列表必须复制，避免后续任务修改已经写入的历史内容。

## 持久化与兼容性

`services/update_history.py` 的规范化逻辑新增：

- 顶层 `status` 接受 `success`、`partial_success`、`failed`，其他值仍规范化为 `failed`。
- 顶层新增 `cluster_results`，只保留上述白名单字段并规范化类型。
- 旧记录缺少 `cluster_results` 时返回空数组。
- 非 K8S 更新默认保存空数组，页面不显示集群区域。

`update_history.json` 继续使用当前版本和保留数量，不做批量迁移。读取旧文件后再次写入时，旧记录会经规范化自然补上空数组。

## 页面展示

K8S 更新历史卡片继续显示来源、窗口、时间和资源统计，并在正文下增加逐集群列表：

```text
cluster-a  成功  32 个 Workload  12.4 秒
cluster-b  失败  Prometheus timeout
```

展示规则：

- `success` 显示“成功”，使用现有成功色。
- `partial_success` 显示“部分成功”，卡片和状态标识使用警告色。
- `failed` 显示“失败”，使用现有失败色。
- 集群成功项显示 Workload 数和耗时；失败项显示错误，存在耗时时同时显示耗时。
- 所有动态文本继续经过 `escapeHtml()`。
- `cluster_results` 为空时不渲染集群列表，保持 VM 和旧记录页面紧凑。

更新状态区域不新增常驻布局字段；任务完成后仍通过历史卡片查看逐集群详情，避免扩大本次范围。

## 错误处理

- 单集群异常只写入对应 `cluster_results[].error`；只要还有成功集群，继续处理成功数据。
- 全部失败时，顶层错误汇总各失败集群的简短错误，便于既看总体失败原因又查看逐集群明细。
- 历史写入失败仍只记录日志，不改变拉取、upsert 或预测结果。
- 错误文本不主动包含 Bearer Token、Basic Auth 或完整请求头；沿用现有异常字符串时不得拼接认证配置。

## 测试策略

Provider 测试覆盖：

- 两个集群全部成功，逐集群资源数正确。
- 一个成功、一个异常，保留成功 items 和两条结果。
- 所有集群异常，返回完整失败结果。
- 集群成功但无 Workload 时记为失败。
- 请求了未配置集群时，该集群记为失败且其他集群继续。
- `fail_fast=True` 时剩余集群产生明确失败记录但不发起请求。
- 原有列表 Provider 仍只返回 items。

更新历史测试覆盖：

- `partial_success` 不被规范化为 `failed`。
- `cluster_results` 字段类型、错误和计数得到规范化。
- 旧记录与非 K8S 记录返回空数组。
- K8S 外部更新成功、部分成功和失败只写一条终态记录。

页面测试覆盖脚本包含“部分成功”文案、逐集群渲染和 HTML 转义路径。相关 Python 修改完成后按项目约定运行针对性测试；由于改动涉及 Provider、数据更新核心和历史输出，最终运行完整回归检查：`compileall`、`pyflakes`、`vulture --min-confidence 80` 和 `pytest -q`，全部使用 `\.venv\Scripts\python.exe`。测试产生的项目 `__pycache__` 在结束后清理，`.venv` 内缓存不处理。

## 文档同步

更新 `docs/api-reference.md`，补充 `/api/update-history` 的 `partial_success` 和 `cluster_results` 响应结构。更新 `docs/architecture.md` 的更新历史说明，明确 K8S 多集群拉取会记录逐集群结果。README 保持快速入门定位，不增加详细字段表。
