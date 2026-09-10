# 本地数据共享 LSTM 实验

独立研究入口，不接入在线选型或扩缩容。仅读取已有 `raw_index.json + raw/`，不重新拉数据、不修改原始文件。
每次按一种资源类型、指标和层级训练一个共享单变量 LSTM。默认只使用历史目标值，无资源 ID embedding 或外部特征。

首轮运行结果和证据边界见 [RESULTS.md](RESULTS.md)。

**在内网读取内网现有数据训练：见 [DEPLOY.md](DEPLOY.md)，包含源码部署、阿里云依赖下载和完全离线安装步骤。**

已确认目标为 CentOS 7.6 / glibc 2.17 / x86_64 / Python 3.10.13：使用 `requirements-centos7.txt` 和 DEPLOY.md 顶部专用步骤，不使用下方通用依赖安装命令。

## 安装和运行

在项目根目录执行，先安装项目原有依赖，再安装训练依赖。默认使用 CPU 训练，安装示例：

```bash
source .venv/bin/activate
python -m pip install -r training/requirements.txt --index-url https://mirrors.aliyun.com/pypi/simple/

# VM CPU：固定种子抽取最多 100 个资源，预测未来 24 小时
python -m training.lstm --raw outputs/vm --out outputs/lstm/vm-cpu-01 \
  --resource-type openstack_vm --metric cpu

# K8S 容器 CPU：也支持 memory_request / cpu_limit / memory_limit
python -m training.lstm --raw outputs/k8s --out outputs/lstm/container-cpu-01 \
  --resource-type k8s_workload --level container --metric cpu_request

# CPU 小规模流程验证（结论不能用于评价实际准确率）
python -m training.lstm --raw outputs/vm --out outputs/lstm/smoke-01 \
  --resource-type openstack_vm --metric cpu --max-resources 4 --epochs 2 \
  --baselines seasonal_naive rolling_mean
```

Windows 使用 `.\.venv\Scripts\python.exe -X utf8 -m training.lstm ...`。
`--raw` 指向包含索引的 scope 目录（也可为现有离线包中的同格式目录），不能指向预测详情或图表文件。
输出目录必须不存在，防止覆盖已有实验。不会自动下载数据、生成替代数据或启动采集服务。

## 数据与验证

- 默认输入 24 小时，输出 24 小时；10 分钟数据对应 144 输入点和 144 输出点。
- 所有资源共用训练/验证/测试截止时间。默认最新 24 小时为测试，再前 24 小时为验证，其余为训练。
- 可显式设置 `--train-end 2026-08-20T00:00:00 --validation-end 2026-08-21T00:00:00`；使用与 raw 一致的时间基准。
- 训练标签全部在训练截止点之前；每条序列的均值/标准差只使用训练段。验证早停按归一化 MSE，最多 20 轮、耐心值 3。
- 测试窗口不重叠；多个测试起点会使用起点之前已经到达的观测，LSTM 权重和归一化保持不变。这是滚动起点评价，不是从单一起点预测整个测试期。
- 不在测试后重新训练，不用测试选择超参数。若反复查看测试结果修改模型，该时间段应标为开发数据，另留未来时间段最终确认。
- 不插值。缺口、不同采样间隔、历史不足会被记录或明确报错。原有 raw 解码器会去除 NaN/重复点；读取后检查规则网格，不跨缺口生成滑窗。
- 容器模式只读取容器指标，不把 Workload 聚合值与容器混合。归一化参数按资源/容器保存；未见过的新资源尚无生产推理契约。
- 原始序列驻留内存，滑窗按需生成；`--max-resources 100` 限制首轮规模，`0` 表示全量。万级资源应先测峰值内存和吞吐，不能由小试跑推断容量。

## 参数与对比

默认 hidden-size=32、layers=1、dropout=0.1、learning-rate=0.001、batch-size=64、stride=6（点）。
可通过同名 CLI 参数调整。`--threads 2` 控制 CPU 线程；CPU 为默认，GPU 环境安装匹配版本并用 `--device cuda`。
GPU 确定性计算可能需要启动前设置 `CUBLAS_WORKSPACE_CONFIG=:4096:8`，不保证跨硬件逐位一致。

默认比较 Seasonal Naive、Rolling Mean、Prophet；可以通过 `--baselines` 缩小范围。各基线在同一测试起点、相同可见历史上预测；Prophet 固定项目默认参数且不经路由。
LSTM 与基线均按项目规则裁剪预测到非负及历史自适应上限。预测只使用给定观测，无法预知部署或突发事件。
初版不自动调参、不自动判定上线赢家。误差越低不一定容量风险越低，需同时检查峰值低估。

## 产物

| 文件 | 内容 |
| --- | --- |
| `model.pt` | 权重、网络结构、lookback、归一化参数、时间边界、配置、输入指纹；不是生产模型 |
| `report.json` | 训练曲线、最佳 epoch、跳过原因、版本、窗口数、各模型平均窗口误差和耗时 |
| `forecast_error_report.json` | 独立测试逐资源/容器/指标/模型/窗口 RMSE、MAE、MAPE、P95 绝对误差及低估指标 |
| `predictions.jsonl` | 每个测试窗口的真实值和各模型预测，时间由窗口起点及报告采样间隔恢复 |

MAPE 是比例值，真实值接近零时可能很大；P95 error 是误差分位数，不是使用率 P95。
汇总为各窗口误差等权平均，并非合并所有点后的 RMSE/P95。失败基线记录单独计数；存在失败时，汇总不保证样本配对，需按明细取共同成功窗口再比较。
训练损失经过逐序列标准化，汇总测试误差保留原指标尺度。计时区分训练、LSTM 批量推理和基线每窗口拟合预测，不能将一次共享训练费用与单资源基线直接作比。

加载已生成的本地模型：

```python
import torch
from training.lstm import SharedLSTM

checkpoint = torch.load("outputs/lstm/vm-cpu-01/model.pt", map_location="cpu", weights_only=True)
model = SharedLSTM(**checkpoint["architecture"])
model.load_state_dict(checkpoint["state_dict"])
model.eval()
# 输入 [batch, lookback, 1]，按对应 scaler 标准化；输出需还原尺度并执行项目裁剪。
```

检查：`python -m pytest -q tests/test_lstm_training.py`。未安装 torch 时仍检查数据边界，训练集成测试会跳过。
