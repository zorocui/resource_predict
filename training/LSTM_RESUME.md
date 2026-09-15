# LSTM 流式读取与批次级断点续训

适用新版共享 LSTM，CPU/内存 request/limit 四类序列仍共用一个模型。仍仅读取内网已有 raw，不重新拉数据。

## 全量训练

使用已安装 torch 的内网 Python 环境，在新版源码根目录执行：

```bash
python -u -m training.lstm \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/lstm/all-stream-01 \
  --resource-type k8s_workload --level container \
  --max-resources 0 --epochs 20 --batch-size 64 \
  --checkpoint-every 1000 \
  --baselines seasonal_naive rolling_mean
```

第一次执行先逐资源生成 `data-cache/` 磁盘缓存；资源类型、指标、采样间隔和时间切分规则不变。
`--max-resources 0` 为全部。建议仍先小范围测吞吐和磁盘容量，再开始全量。

## 中断后继续

相同参数、相同输出目录，追加 `--resume`：

```bash
python -u -m training.lstm \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/lstm/all-stream-01 \
  --resource-type k8s_workload --level container \
  --max-resources 0 --epochs 20 --batch-size 64 \
  --checkpoint-every 1000 --resume \
  --baselines seasonal_naive rolling_mean
```

`resume.pt` 原子保存当前网络权重、Adam 优化器状态、当前 epoch、下一个批次位置、本轮累计损失、最佳模型、早停计数、已完成轮次历史，以及 Python/NumPy/torch/CUDA 随机状态。
默认每 1000 个批次保存一次，训练轮次结束和验证结束也保存。进程被终止时，从最近成功提交的检查点恢复，最多重做尚未提交的一段批次；不是从头训练整个模型。
更频繁保存可用 `--checkpoint-every 100` 或 `1`，但会增加磁盘写入；续训时允许改变这一保存频率。

验证阶段中断会重新执行该次验证，已提交的本轮梯度更新不重复。测试/基线报告阶段中断会从最佳权重重新生成评价产物，不重训网络；尚未完成报告时请先查看 status.json。
数据缓存准备中断会重新扫描准备缓存；缓存完成前尚未开始梯度训练。

## 数据与兼容限制

- 使用稳定的内网 raw 快照，不要让采集程序持续改动同一个输入目录。续训要求原 raw 索引校验值不变，并检查缓存文件哈希；数据/缓存变化会拒绝恢复。
- 归一化仍按资源—容器—指标分别计算，仅使用训练段。所有序列共用训练/验证/测试截止时间。
- batch-size、模型结构、学习率、种子、资源范围、窗口、设备/线程配置、PyTorch 版本或 Prophet 基线版本改变时拒绝续训；要做新实验请新建输出目录。
- 允许提高 `--epochs` 上限，也允许修改检查点频率；已经触发早停的模型仍遵守原早停条件。不要根据反复查看的测试误差挑选训练轮数，最终评估另留时间段。
- `model.pt` 是最佳验证模型，用于后续加载；`resume.pt` 是完整训练状态，二者用途不同。旧版只有 model.pt 的实验不能精确续训，需要新建实验目录。
- 新导出模型版本为 `shared-lstm-experiment-v3`，网络输入/输出形状和模型加载方法保持一致。
- 输出目录使用操作系统锁，同一目录只能有一个训练进程。恢复前停止原进程，不要通过删除锁文件绕过互斥。
- CPU 上已验证批次中断恢复和不中断训练逐项一致；跨机器、不同 GPU/库版本的逐位一致性未验证。

## 内存、磁盘和随机顺序

原始时序逐资源解码并保存为 float64 `.npy`；训练只映射少量序列，默认保留 4 个映射，按批次做标准化。
索引每条序列只存 range 和累计窗口数，不构造全部训练窗口或全局窗口排列。每轮打乱序列顺序，再打乱当前序列内的窗口，改善磁盘局部性；顺序不同于旧版全局随机窗口，所以不是复现旧版实验的相同训练轨迹。
内存仍包含资源/序列元数据、当前资源解码、单序列窗口排列、当前批次和评价误差明细；不是严格常量内存，也不会缩短必要的梯度计算。
例如 3000 Workload × 2 容器 × 4 指标 × 2160 点，缓存纯数值约 415 MB（十进制），另有文件头/索引和报告。系统可能用空闲内存缓存映射文件，这是可回收的页缓存。
请保留整个实验输出目录，尤其 `data-cache/`、`run.json` 和 `resume.pt`；单独拷贝 model.pt 无法恢复训练。

检查：`python -m pytest -q tests/test_lstm_resume.py tests/test_lstm_training.py`。
