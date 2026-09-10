# 在内网使用既有数据训练 LSTM

目的：把训练源码和依赖搬到内网，直接读取内网现有的 raw 数据。无需重新采集，也无需把内网数据传到外网。
本机 RESULTS.md 只是流程验证，不代表内网数据实验结果。

## 已确认的目标：CentOS 7.6 / glibc 2.17 / x86_64 / Python 3.10.13

用户已确认内网安装 torch 2.6.0。**优先复用安装它的现有 Python 环境，无需重新安装 torch，也不要先创建一个看不到现有依赖的新虚拟环境。**
在该环境下执行以下检查，确认“pip 已安装”同时满足运行时导入及 LSTM 前向计算可用：

```bash
python -c "import sys, torch, numpy, pandas; print(sys.executable); print(torch.__version__); print(torch.nn.LSTM(1, 4, batch_first=True)(torch.zeros(2, 24, 1))[0].shape)"
```

预期 torch 为 2.6.0（允许构建后缀），最后输出 `torch.Size([2, 24, 4])`。
通过后直接解压源码，执行第 4 节小规模命令即可，跳过本页所有安装步骤。
若 NumPy/Pandas 缺失，只补缺少的依赖；若 torch 导入报 GLIBC/GLIBCXX 错误，先解决当前安装的 wheel/运行库匹配问题，不能仅凭 pip 列表判断兼容。
下方安装清单仅供环境缺包或需要新建隔离环境时使用。

**该机器使用 `training/requirements-centos7.txt`，不要使用下面通用安装中的无上限项目依赖或默认 torch 范围。**
已核对阿里云镜像存在 `torch-2.6.0-cp310-cp310-manylinux1_x86_64.whl`，以及 NumPy 1.26.4、Pandas 2.2.3 的 cp310 manylinux_2_17 wheel。
PyTorch 2.6 的部分其他构建已转向 glibc 2.28，2.7 起 Linux 构建迁移到新平台；不能把“2.6”泛化为所有构建均兼容。
这里固定阿里云标准 PyPI 中上述 x86_64 wheel；仍需在目标机实际导入和小训练验收，本机 Windows 测试不能替代该验收。
参考：[PyTorch 2.6 官方说明](https://pytorch.org/blog/pytorch2-6/)、[Linux wheel 平台迁移说明](https://dev-discuss.pytorch.org/t/pytorch-linux-wheels-switching-to-new-wheel-build-platform-manylinux-2-28-on-november-12-2024/2581)。

若内网可访问阿里云，在解压后的独立目录执行：

```bash
cd /opt/lstm-training
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --only-binary=:all: \
  --index-url https://mirrors.aliyun.com/pypi/simple/ \
  -r training/requirements-centos7.txt
python -m pip check
python -c "import torch, numpy, pandas; print(torch.__version__); print(torch.randn(2, 3).sum())"
python -m training.lstm --help
```

若完全离线，在能联网的同类 CentOS 7 x86_64 + Python 3.10 环境准备：

```bash
python -m pip download --only-binary=:all: \
  --index-url https://mirrors.aliyun.com/pypi/simple/ \
  -r training/requirements-centos7.txt -d wheelhouse
tar -czf lstm-centos7-py310-wheelhouse.tar.gz wheelhouse
```

带入内网，创建并激活上述独立 `.venv` 后执行：

```bash
tar -xzf /path/to/lstm-centos7-py310-wheelhouse.tar.gz
python -m pip install --no-index --find-links=wheelhouse -r training/requirements-centos7.txt
python -m pip check
python -c "import torch, numpy, pandas; print(torch.__version__); print(torch.randn(2, 3).sum())"
```

必须带上整个 wheelhouse（含所有传递依赖），不能只拷贝 torch。此镜像 torch wheel 会带入 CUDA 库依赖，下载体积较大；CPU 训练无需 GPU 或 CUDA 驱动。
首轮训练在第 4 节命令上显式追加 `--baselines seasonal_naive rolling_mean`。最小清单没有安装 Prophet；如要对比 Prophet，再单独准备与目标系统兼容的依赖，不要直接安装其最新依赖组合。
阿里云已核对存在 Prophet 1.1.6 的 manylinux_2_17 wheel，但其完整依赖环境与 Stan 执行仍需目标机验收。
如只有 Windows 联网机器，不能直接执行普通 `pip download` 得到 Linux 完整包；优先在匹配目标环境的 Linux 虚拟机/容器准备。

## 1. 部署目录

使用独立源码包 `lstm-training-source.zip`，解压为 `lstm-training/`。包内包含 `training/`、项目 Python 公共模块、依赖声明和文件校验清单；不含数据、集群配置、凭据、本机虚拟环境或模型。
可把它上传到 `/opt/lstm-training-source.zip`（路径按实际调整），在内网执行：

```bash
cd /opt
unzip lstm-training-source.zip
cd /opt/lstm-training
```

原预测系统继续放在原目录。训练命令通过绝对 `--raw` 路径读取原系统数据，结果写入新目录。
如内网已有相同版本项目源码，也可仅同步 `training/` 后在该项目根目录运行；独立源码包能避免公共模块版本不一致。

## 2. 环境确认与依赖

在内网机器执行，记录输出：

```bash
cat /etc/os-release
uname -m
python --version
ldd --version | head -n 1
```

本次已验证 Windows Python 3.10 + PyTorch 2.14.0。内网 Linux 尚未验证。
建议使用 Python 3.10 或更新且受所选 PyTorch wheel 支持的版本；不能用项目旧 README 的 Python 3.8 下限推断训练可运行。
Linux wheel 还依赖架构、glibc 和运行库。尤其旧 CentOS 需先核实兼容性；不要复制 Windows 的 `.venv` 或 `.whl` 到 Linux 安装。
如果 wheel 与宿主不兼容，应使用兼容的训练节点/容器或经过验证的依赖组合，不建议修改生产宿主的系统 glibc。

### 内网能访问阿里云镜像

在独立训练目录创建独立虚拟环境，不升级生产服务依赖。下面假定 `python3.10` 已安装：

```bash
cd /opt/lstm-training
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r training/requirements.txt \
  --index-url https://mirrors.aliyun.com/pypi/simple/
python -c "import torch, pandas, numpy; print(torch.__version__, torch.cuda.is_available())"
python -m training.lstm --help
```

### 完全离线内网

在一台能联网且与内网匹配的 **Linux 系统/容器** 中准备 wheelhouse，至少匹配 CPU 架构、Python 小版本，并确保其 glibc 兼容目标机器。所有下载使用阿里云：

```bash
cd /path/to/lstm-training
python3.10 -m venv .download-venv
source .download-venv/bin/activate
python -m pip download --only-binary=:all: \
  --index-url https://mirrors.aliyun.com/pypi/simple/ \
  -r requirements.txt -r training/requirements.txt -d wheelhouse
tar -czf lstm-wheelhouse.tar.gz wheelhouse
```

若提示没有匹配发行包，先解决目标环境兼容性或镜像缺包问题；不静默换境外源、不在内网临时编译大依赖。
上述 Linux 下载可能包含较大的 GPU 相关依赖，具体由镜像所提供 wheel 决定；程序默认 CPU 运行，并不要求安装 GPU。

通过内部允许的文件传输方式，把源码 ZIP 和 `lstm-wheelhouse.tar.gz` 一起带入内网。然后：

```bash
cd /opt/lstm-training
tar -xzf /path/to/lstm-wheelhouse.tar.gz
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --find-links=wheelhouse \
  -r requirements.txt -r training/requirements.txt
python -m pip check
python -c "import torch, pandas, numpy; print(torch.__version__, torch.cuda.is_available())"
python -m training.lstm --help
```

当前提供的是源码包，不包含尚未确认目标环境的 Linux 依赖包。

## 3. 定位内网现有数据

以下以原系统位于 `/opt/resource_predict` 为例，替换为实际路径：

```bash
ls /opt/resource_predict/outputs/k8s/raw_index.json
ls /opt/resource_predict/outputs/vm/raw_index.json
```

输入目录必须同时包含 `raw_index.json` 和它引用的 `raw/` 分片。仅有 `items.json`、预测报告、图表或旧 raw 格式不能作为本入口输入。
为保证复现，优先使用内网已有的完整离线 raw 快照。若只有持续更新的数据目录，应在采集写入暂停的时段复制索引和分片到新的本地快照目录，复制期间避免原目录变更；这是复制既有数据，不是重新拉取。
不要删除或修改原始数据。源码包无需集群访问配置。

## 4. 先检查少量资源，再正式运行

先用 4 个 Workload 的已有容器数据跑 2 轮，验证安装、格式和时间窗口：

```bash
cd /opt/lstm-training
source .venv/bin/activate
python -u -m training.lstm \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/lstm/k8s-smoke-01 \
  --resource-type k8s_workload --level container --metric cpu_request \
  --max-resources 4 --epochs 2 --baselines seasonal_naive rolling_mean
```

正式首轮取最多 100 个 Workload，20 轮上限，默认比较 Seasonal Naive、Rolling Mean、Prophet：

```bash
mkdir -p outputs/logs
nohup python -u -m training.lstm \
  --raw /opt/resource_predict/outputs/k8s \
  --out outputs/lstm/k8s-cpu-01 \
  --resource-type k8s_workload --level container --metric cpu_request \
  --lookback 24h --horizon 24h --validation-duration 24h --test-duration 24h \
  --max-resources 100 --epochs 20 --threads 2 \
  > outputs/logs/k8s-cpu-01.log 2>&1 &
tail -f outputs/logs/k8s-cpu-01.log
```

VM 改为 `--raw /opt/resource_predict/outputs/vm --resource-type openstack_vm --level resource --metric cpu`。
容器内存改为 `--metric memory_request`，并更换输出目录。每次运行独立一个指标，输出目录必须为新目录。
`--max-resources 0` 是全部资源；先根据首轮内存、训练耗时和可用序列数评估，不建议首次直接跑万级全量。

出现“历史不足”时，根据现有数据长度调整 lookback/验证/测试时长，或选择更长的既有快照；不要为了跑通而让训练、验证和测试标签重叠。
数据时间单位和模型窗口根据 raw 实际采样间隔计算，无需重新配置 Prometheus。

## 5. 看结果

结果位于本次 `--out`：

- `report.json`：可用序列、跳过原因、时间切分、训练曲线、最佳轮次、基线比较和耗时。
- `forecast_error_report.json`：逐资源/容器/指标/模型/窗口误差。
- `predictions.jsonl`：真实值和预测曲线。
- `model.pt`：训练权重与配置，用于复现；不会自动替换在线模型。

先检查失败和跳过计数，再比较相同成功窗口上的 RMSE、P95 误差及峰值低估。
模型、报告和原始数据均留在内网即可。报告含资源标识与预测值，不必为了训练把这些文件传到外网。
