# 本地数据共享 LSTM 实验

完整操作流程见 [Prophet 与 LSTM 训练及准确性验证指南](../docs/model-training-validation.md)：包含 15 天历史的时间切分、两模型训练命令、配对误差比较和未来验证边界。

训练入口独立运行，仅读取已有 `raw_index.json + raw/`，不重新拉数据、不修改原始文件。训练好的 model.pt 现可通过在线配置注册为 LSTM 候选，见 [在线接入说明](../docs/lstm-online.md)，不会自动启用。
每次按一种资源类型和层级训练一个共享 LSTM，默认同时学习该类型的全部指标。K8S 的 CPU/内存 request/limit 四类序列共用一套网络权重，一次训练、批量预测，输出保留各指标原口径。
每个窗口仍以一条序列的历史值为输入，不是把四类曲线相加/平均，也不是四通道联合输入；不使用指标名称 embedding 或外部特征。

首轮运行结果和证据边界见 [RESULTS.md](RESULTS.md)。

**Prophet 也可先训练保存、后续只加载预测：见 [PROPHET.md](PROPHET.md)。** LSTM 通过 `--prophet-models` 使用冻结模型作为对照，不再每个测试窗口从头拟合。
Prophet 已支持流式训练和 `--resume` 断点续训；全量用 `--max-resources 0`，中断后保持原输出目录并追加 `--resume`。
**LSTM 也已支持磁盘流式读取和批次级 `--resume` 续训**，完整命令和恢复限制见 [LSTM_RESUME.md](LSTM_RESUME.md)。

**在内网读取内网现有数据训练：见 [DEPLOY.md](DEPLOY.md)，包含源码部署、阿里云依赖下载和完全离线安装步骤。**

已确认目标为 CentOS 7.6 / glibc 2.17 / x86_64 / Python 3.10.13：使用 `requirements-centos7.txt` 和 DEPLOY.md 顶部专用步骤，不使用下方通用依赖安装命令。

## 安装和运行

在项目根目录执行，先安装项目原有依赖，再安装训练依赖。默认使用 CPU 训练，安装示例：

```bash
source .venv/bin/activate
python -m pip install -r training/requirements.txt --index-url https://mirrors.aliyun.com/pypi/simple/

# VM 全指标：CPU/内存/磁盘共用一个模型
python -m training.lstm --raw outputs/vm --out outputs/lstm/vm-all-01 \
  --resource-type openstack_vm

# K8S 容器四类指标：一次训练、一个 model.pt、批量输出各自预测
python -m training.lstm --raw outputs/k8s --out outputs/lstm/container-all-01 \
  --resource-type k8s_workload --level container

# CPU 小规模流程验证（结论不能用于评价实际准确率）
python -m training.lstm --raw outputs/vm --out outputs/lstm/smoke-01 \
  --resource-type openstack_vm --max-resources 4 --epochs 2 \
  --baselines seasonal_naive rolling_mean
```

Windows 使用 `.\.venv\Scripts\python.exe -X utf8 -m training.lstm ...`。
`--raw` 指向包含索引的 scope 目录（也可为现有离线包中的同格式目录），不能指向预测详情或图表文件。
首次运行输出目录必须不存在；续训保持同一目录并加 `--resume`。不会自动下载数据、生成替代数据或启动采集服务。

## 数据与验证

- 默认输入 24 小时，输出 24 小时；10 分钟数据对应 144 输入点和 144 输出点。
- 所有资源共用训练/验证/测试截止时间。默认最新 24 小时为测试，再前 24 小时为验证，其余为训练。
- 可显式设置 `--train-end 2026-08-20T00:00:00 --validation-end 2026-08-21T00:00:00`；使用与 raw 一致的时间基准。
- 训练标签全部在训练截止点之前；均值/标准差按资源—容器—指标分别计算，只使用训练段。验证早停按全部序列的归一化 MSE，最多 20 轮、耐心值 3。
- 测试窗口不重叠；多个测试起点会使用起点之前已经到达的观测，LSTM 权重和归一化保持不变。这是滚动起点评价，不是从单一起点预测整个测试期。
- 不在测试后重新训练，不用测试选择超参数。若反复查看测试结果修改模型，该时间段应标为开发数据，另留未来时间段最终确认。
- 不插值。缺口、不同采样间隔、历史不足会被记录或明确报错。原有 raw 解码器会去除 NaN/重复点；读取后检查规则网格，不跨缺口生成滑窗。
- 容器模式只读取容器指标，不把 Workload 聚合值与容器混合。归一化参数按资源/容器保存；未见过的新资源尚无生产推理契约。
- 原始序列逐资源写入 `data-cache/`，训练按需映射少量序列和窗口；`--max-resources 100` 限制首轮规模，`0` 表示全量。索引和评价明细仍占内存，万级资源需先测内存、磁盘和吞吐。

## 参数与对比

默认 hidden-size=32、layers=1、dropout=0.1、learning-rate=0.001、batch-size=64、stride=6（点）。
`--metric` 默认 `all`，不再需要分四次执行；保留 `--metric cpu_request` 等单指标选项用于对照实验。缺失的指标记录跳过原因，其他可用指标仍可参与训练。
可通过同名 CLI 参数调整。`--threads 2` 控制 CPU 线程；CPU 为默认，GPU 环境安装匹配版本并用 `--device cuda`。
GPU 确定性计算可能需要启动前设置 `CUBLAS_WORKSPACE_CONFIG=:4096:8`，不保证跨硬件逐位一致。

默认比较 Seasonal Naive、Rolling Mean、Prophet；可以通过 `--baselines` 缩小范围。各基线在同一测试起点、相同可见历史上预测；Prophet 固定项目默认参数且不经路由。
LSTM 与基线均按项目规则裁剪预测到非负及历史自适应上限。预测只使用给定观测，无法预知部署或突发事件。
初版不自动调参、不自动判定上线赢家。误差越低不一定容量风险越低，需同时检查峰值低估。

## 产物

| 文件 | 内容 |
| --- | --- |
| `model.pt` | 权重、网络结构、lookback、归一化参数、时间边界、配置、输入指纹；可显式配置为在线候选 |
| `resume.pt` | 当前权重、优化器、批次/轮次进度、随机状态及最佳验证状态，用于 --resume |
| `data-cache/`、`run.json`、`status.json` | 冻结磁盘数据、续训配置、任务是否完成；恢复时需保留 |
| `report.json` | 训练曲线、最佳 epoch、跳过原因、版本、窗口数、`series_by_metric`、`summary_by_metric` 分指标误差和耗时 |
| `forecast_error_report.json` | 独立测试逐资源/容器/指标/模型/窗口 RMSE、MAE、MAPE、P95 绝对误差及低估指标 |
| `predictions.jsonl` | 每个测试窗口的真实值和各模型预测，时间由窗口起点及报告采样间隔恢复 |

MAPE 是比例值，真实值接近零时可能很大；P95 error 是误差分位数，不是使用率 P95。
汇总为各窗口误差等权平均，并非合并所有点后的 RMSE/P95。失败基线记录单独计数；存在失败时，汇总不保证样本配对，需按明细取共同成功窗口再比较。
统一模式优先查看 `summary_by_metric`；旧 `summary` 保留为跨指标诊断，不用它代表某个指标精度。归一化参数在 model.pt 中按资源—容器—指标定位，版本为 shared-lstm-experiment-v3。
request/limit 是原数据的不同分母，仍分别保存预测和误差；未承诺二者预测严格成比例。统一模型也不保证优于分别训练，需比较分指标验证和测试表现。
训练损失经过逐序列标准化，汇总测试误差保留原指标尺度。计时区分训练、LSTM 批量推理和基线每窗口拟合预测，不能将一次共享训练费用与单资源基线直接作比。

加载已生成的本地模型：

```python
import torch
from training.lstm import SharedLSTM

checkpoint = torch.load("outputs/lstm/vm-all-01/model.pt", map_location="cpu", weights_only=True)
model = SharedLSTM(**checkpoint["architecture"])
model.load_state_dict(checkpoint["state_dict"])
model.eval()
# 输入 [batch, lookback, 1]，按对应 scaler 标准化；输出需还原尺度并执行项目裁剪。
```

检查：`python -m pytest -q tests/test_lstm_training.py`。未安装 torch 时仍检查数据边界，训练集成测试会跳过。
