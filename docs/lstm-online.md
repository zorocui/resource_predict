# 已训练 LSTM 接入在线候选预测

已支持把 `training.lstm` 生成的 `model.pt` 注册为原系统候选模型。在线仅读取已有权重与归一化参数，不训练、不更新优化器，也不自动切换为共享 Prophet。
默认不启用 LSTM，不改动已有资源配置和扩缩容门槛。

## 1. 内网部署与启用

更新项目代码（包含 `resource_predict/`、`static/js/` 和训练共用的网络定义）。将内网已训练的 **model.pt** 放到在线服务进程可读取的稳定路径，例如 `/opt/resource_predict/models/lstm-v1/model.pt`。
不使用 `resume.pt`，也不需要把 `data-cache/` 复制给在线预测服务。支持当前训练产物 v2/v3；旧文件缺少指标身份或时间边界时会明确失败。

确认运行在线系统的 Python 环境有 torch，注意训练环境和 Web 服务环境可能不同：

```bash
python -c "import torch; print(torch.__version__)"
```

已有依赖无需重装。CentOS 7 / Python 3.10 缺包时按 [训练部署说明](../training/DEPLOY.md) 核对兼容性，并使用阿里云镜像；未启用 LSTM 的部署不要求安装 torch。

在页面“系统配置 → 预测配置”中：

1. 勾选 **LSTM（已训练模型）**，同时保留至少一个轻量基线用于对照。
2. 填写 **LSTM 模型文件路径**，指向 model.pt。
3. 设置 **最大训练滞后小时数**，默认 168 小时，0 表示不限制。
4. 保存后进行正常数据更新或仅重新预测。

也可在服务停止时编辑 `deploy/runtime_config.json` 的 prediction 分区并重启服务，保留其余字段。例如需要合并的字段为：

```json
{
  "enabled_methods": ["seasonal_naive", "rolling_mean", "prophet", "lstm"],
  "lstm_model_path": "/opt/resource_predict/models/lstm-v1/model.pt",
  "lstm_max_age_hours": 168
}
```

这是 prediction 内部字段示例，不是整份 runtime_config.json。保留 Prophet 表示它继续遵循现有路由/重新拟合行为；想单独比较 LSTM 与轻量基线可以不启用 Prophet。

## 2. 在线如何使用模型

每条资源—容器—指标通过完整身份查找对应的离线归一化参数；取最近 lookback 个观测，生成未来曲线，再还原原尺度并按项目规则裁剪。
CPU/内存四类指标仍共享一个网络。当前单个配置指向一个 model.pt，文件的资源类型和层级必须匹配；容器模型不能直接替代 Workload 聚合指标模型。
未知资源/容器/指标没有 scaler 时，此 LSTM 候选失败，其他候选继续处理；不会临时用当前测试标签估计归一化参数。

检查项目包括模型格式/权重、采样间隔、输入连续性、lookback、输出长度、模型滞后和资源身份。错误按阶段写入诊断及误差报告。
LSTM 固定输出长度由训练决定：在线验证、测试、未来步数都不能超过训练 horizon。小于 horizon 时取预测前缀，不递推拼接更长预测。
例如训练输出 24 小时，VM 测试窗口仍为 72 小时时，LSTM 没有可用验证分数，不能成为最佳候选；应使用适合的验证窗口或训练更长 horizon。

模型滞后按“当前可见观测末尾 − 模型训练截止时间”计算，不使用文件复制时间。超过限制时需要离线重训并发布新版本，不进行在线 fit。

## 3. 怎样参与选型，怎样避免泄漏

LSTM 与其他候选遵循当前验证选型规则：必须在训练段内部验证的全部可用折上成功。利用率按验证容差达标率竞争，相同时比较 selection_rmse；绝对使用量仍按 selection_rmse，并保留异常序列的鲁棒候选优先规则。
**离线最佳 epoch 使用过验证标签，所以在线验证/测试不仅要晚于离线训练段，还必须不早于离线 `validation_end_exclusive`。**

例如离线使用第 1～11 天训练、第 12～13 天挑 epoch，那么在线可以用第 14 天做候选验证、第 15 天做独立测试；不能拿第 12～13 天再次给这个模型算独立验证分数。
目前使用的训练模型 config/boundaries/scalers 会随 model.pt 一起保存，无需手工填写边界。

若 LSTM 能生成未来曲线但没有合法验证分数，它仍可出现在候选曲线中，但不会直接当作最佳模型，也不参加误差权重集成。
仅启用 LSTM 而它没有合法选型证据时，会使用 Rolling Mean 兜底；若选中模型的未来预测失败，也沿用现有 Rolling Mean 兜底行为。
外层测试标签仍不用于模型选型或集成定权重。不会借用另一版本模型的误差作为当前模型的验证证据。

## 4. 查看接入结果

- 图表图例与准确性页面显示 `LSTM`。
- 原有 `best_methods` / 容器最佳模型、未来曲线和耗时记录包含 lstm。
- `forecast_error_report.json` 按资源、容器、指标、模型、窗口继续记录 RMSE/MAE/MAPE/P95 error；失败阶段保留原因，没有有效预测时不伪造零误差。
- 对应 `forecast_diagnostics` 中的 `saved_lstm` 提供模型 SHA256、离线时间边界、是否具备选型资格和实际只推理标记。
- `provenance.model_training.lstm` 记录固定模型的训练截止及离线选型截止。LSTM 被选中时，顶层 train_end_ms 标记为截止点前一毫秒的上界，并附 `train_end_is_upper_bound=true`；不能解释成每条序列最后一个实际训练样本时间。

预测接口、产物目录和更新任务入口不变。原有 action_gate、confidence、data_quality、cooldown 和 policy_tier 执行检查全部保留。

## 5. 模型版本、缓存与当前限制

每轮开始读取 model.pt 的 SHA256，所有 worker 使用这一固定版本。每进程最多缓存两个已加载模型，scaler 建立字典索引，不为每条序列遍历全量映射。
更新文件后下一轮读取新哈希；当前轮已加载的旧权重继续使用，尚未加载的 worker 若发现文件被替换会报告候选失败，不混用新文件。
建议将每个模型放到独立版本目录，通过配置切换路径，保留旧版本以便回退。

在线推理使用 CPU，torch 内部线程设为 1，复用原有有界任务队列；当前逐指标推理，尚未把跨资源任务合并成一个张量批次。多个进程各持一份权重和 scaler，需结合资源规模评估内存与吞吐。

当前历史模型未保存逐资源训练时的完整 request/limit 规格与指标 mode，不能保证自动检测规格/单位变化，诊断会标记 `metric_semantics_verified=false`。
必须使用相同资源 ID、层级、指标口径的数据；规格或比例分母发生变化时，应重新验证并在必要时离线重训。现有数据质量与执行门槛继续有效，但不能把它们当作完整的单位变化检测器。

首轮建议先观察分指标验证误差、候选失败率和整体预测耗时，再决定扩大使用范围。加载成功并不代表 LSTM 一定比现有基线准确。

相关检查：`python -m pytest -q tests/test_online_lstm.py tests/test_forecast_evaluation.py tests/test_runtime_config.py`。
