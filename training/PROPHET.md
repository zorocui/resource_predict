# Prophet 先训练保存，再加载预测

此次仅用于 training 实验和独立预测。原在线系统保持原流程，没有自动切换到本目录的模型。
原始数据始终使用内网既有 raw，不拉取、不修改。无需 GPU 或 torch，复用项目已有 Prophet 环境。

Prophet 仍按资源—容器—指标分别拟合，不是 LSTM 的跨序列共享网络。默认一次命令训练所有指标，并存为一批模型。
保存使用 Prophet 官方 `model_to_json` / `model_from_json`，不使用 pickle；见 [官方说明](https://facebook.github.io/prophet/docs/additional_topics.html#saving-models)。

## 1. 批量训练并保存

在源码包根目录、已安装 Prophet 的内网 Python 环境运行：

```bash
python -c "import prophet; print(prophet.__version__)"
python -u -m training.prophet train \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/prophet/models-01 \
  --resource-type k8s_workload --level container \
  --max-resources 100
```

省略 `--metric` 表示四类指标全部训练；仍可指定单一指标。VM 使用 `--resource-type openstack_vm --level resource`。
每个成功模型保存为 `models/<哈希>.json`，`manifest.json` 保存资源/容器/指标映射、训练末尾、配置、采样间隔、模型哈希、训练耗时和失败记录。
模型包含拟合所用历史，文件也留在内网。训练是逐序列执行，默认 100 个资源；先检查运行耗时和内存再扩大。
Prophet 训练现在按资源流式读取，不再先载入全量时序；raw 缓存限制为一个资源。内存仍包含资源索引、模型元数据和当前资源的时序/单模型拟合开销，并非与资源数完全无关。单个超大 Workload 的容器数据仍需整体解码。
训练参数沿用项目 Prophet 默认值，关闭未使用的随机区间采样，仅输出点预测。每条序列需至少 48 个有效点。
训练有失败时其余成功模型仍保存，CLI 退出码为 2；检查 manifest 的 failures/skipped。

### 全量训练与断点续训

首次全量训练用 `--max-resources 0`：

```bash
python -u -m training.prophet train \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/prophet/models-all-01 \
  --resource-type k8s_workload --level container --max-resources 0
```

中断后再次执行相同命令，追加 `--resume`，**保持同一个 --out**：

```bash
python -u -m training.prophet train \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/prophet/models-all-01 \
  --resource-type k8s_workload --level container --max-resources 0 --resume
```

- 每条模型先原子写入 `models/`，再提交 `checkpoints/<序列哈希>.json`。即使进程被直接终止，总 manifest 没来得及写完，已提交检查点仍可恢复；未提交的当前模型需要重做。
- 续训仍逐资源读取并计算训练段指纹，但不会对数据、配置和模型校验均通过的序列再次 fit。输出 `trained` 为本次新拟合数、`reused` 为复用数、`available` 为本次清单中的总可用模型数。
- 训练段数据变动、模型/检查点文件损坏时只重训对应序列。`--train-end` 之后的数据变动不影响该训练段的复用。
- `run.json` 记录源路径、模型参数、裁剪配置、seed、截止时间和 Prophet 版本。上述配置不一致时拒绝续训，需使用新目录；资源上限允许从 100 扩大到 0（全部），不能缩小。
- 使用稳定的既有 raw 快照最容易复现。续训按当前快照重新生成最终模型清单，已删除或当前失败的序列不会继续被列为成功；旧模型文件保留，不自动删除。
- 目录有操作系统互斥锁，阻止同时启动两个训练进程。进程退出即释放；不要手动删除 `.training.lock` 来绕过锁。
- 训练运行中或中断后的 manifest 标记 `running/interrupted`，预测入口会拒绝加载。完成本轮扫描后才发布可预测的 `complete` 清单；部分失败仍会记录并以退出码 2 提醒。
- 仅在开始/结束写一次总 manifest，每个模型只提交自己的检查点，避免全量训练时反复重写越来越大的清单。
- 旧版没有 `run.json/checkpoints` 的模型仍可预测，但不能直接续训；请保留旧目录，用新版在新目录开始训练。

Prophet 当前仍为串行训练，尚未添加训练并行。LSTM 的磁盘流式读取和批次续训另见 [LSTM_RESUME.md](LSTM_RESUME.md)。

## 2. 后续只加载预测

```bash
python -u -m training.prophet predict \
  --models outputs/prophet/models-01 \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/prophet/prediction-01 \
  --horizon 24h
```

这条命令 **不调用 fit、不自动重训**。raw 仅确定每条指标最新的时间起点，预测时间紧接该起点，不会错误地从旧模型训练末尾重新生成同一段日期。
不传 `--raw` 时默认紧接训练末尾；也可用 `--after 2026-09-13T00:00:00` 指定起点，两者不能同时使用。
同一命令批量处理所有已保存的模型，采用小型 LRU 控制驻留模型数量；重复命令会重新加载文件，但不会重新训练。

输出 `predictions.jsonl`（毫秒时间戳、原尺度预测、训练截止点和模型滞后时长）以及 `report.json`（成功/失败数、加载预测耗时、fit_calls=0）。
这是真正的未来预测，无真实标签，因此不会编造 RMSE/MAE 或生成伪测试误差报告。
已保存模型缺失、损坏或不匹配时明确失败，不偷偷回退到重新训练；预测有失败时进程退出码为 2。
只处理 manifest 中的模型；后续新增资源需显式执行新训练。

新数据不会自动更新已训练的趋势/季节参数；预测裁剪上限也固定为训练时计算的上限。可以用 `--max-age 24h` 限制“预测起点与训练末尾”的间隔，超出则报告过期。
这是按数据时间计算的模型滞后，不是按文件生成时间。默认不设置过期阈值，但每条预测都记录 model_age_seconds。
需要吸收新数据时重新执行 `train`，保存到 `models-02` 等新目录，然后让 predict 指向新目录。不会覆盖已有模型版本。

## 3. LSTM 实验使用预训练 Prophet 对照

不能把用全部历史训练的模型拿来预测历史测试窗口。先确定 LSTM 的训练截止点，并让 Prophet 使用相同或更早的截止点。
例如以下日期只是占位示例，必须按内网真实数据替换：

```bash
# 仅使用 9 月 10 日零点以前的数据训练 Prophet
python -u -m training.prophet train \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/prophet/baseline-01 \
  --resource-type k8s_workload --level container --max-resources 100 \
  --train-end 2026-09-10T00:00:00

# LSTM 的训练/验证边界显式冻结；复用上面的 Prophet，不再逐窗口拟合
python -u -m training.lstm \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/lstm/all-with-saved-prophet-01 \
  --resource-type k8s_workload --level container --max-resources 100 \
  --train-end 2026-09-10T00:00:00 --validation-end 2026-09-11T00:00:00 \
  --prophet-models outputs/prophet/baseline-01
```

同一冻结 raw 快照、相同资源抽样上限和 seed 保持覆盖一致。`--prophet-models` 会把原本即时拟合的 prophet 基线替换成 `prophet_saved`；即使 baselines 没写 prophet 也会加入该冻结基线。
训练前检查模型身份和训练截止时间；预测时还检查模型实际历史末尾与索引一致。缺少模型或边界过晚则拒绝，不进行无效比较。
分指标比较见 `report.json` 的 `summary_by_metric`，每窗口误差仍写入 `forecast_error_report.json`，`prophet_saved` 明细额外记录 model_train_end。
`prophet_saved` 不吸收验证/测试期间的新观测，而即时拟合的 Prophet 会吸收起点前历史；两种基线含义不同，必须保留独立名称，不能混称同一训练流程。

## 环境和验证

内网已能运行项目 Prophet 时无需另装依赖。缺包时按 DEPLOY.md 的 CentOS 7 兼容步骤和阿里云源准备，不能直接将 Windows wheel 带到内网 Linux。
测试：`python -m pytest -q tests/test_saved_prophet.py tests/test_prophet_resume.py`。测试覆盖禁止预测调用 fit、模型重载、测试泄漏边界、逐资源读取、中断恢复、数据变化/损坏重训和目录互斥锁。
