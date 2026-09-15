# Prophet 与 LSTM：内网训练和准确性验证操作指南

适用：当前项目 `training/` 实验入口，已有数千个 Workload、约 15 天监控数据。
目标环境：CentOS 7.6、glibc 2.17、x86_64、Python 3.10.13，已安装 torch 2.6.0。
本文只使用内网已有数据，不重新采集，不把实验模型自动接入在线预测或扩缩容。

## 1. 先明确训练目标

| 项目 | LSTM | Prophet |
| --- | --- | --- |
| 模型粒度 | 所选资源和四类指标共享一个网络 | 每个资源—容器—指标一个模型 |
| 默认指标 | CPU/内存的 request、limit 四类全部参与 | 四类全部逐条训练 |
| 训练输出 | 一个最佳验证模型 `model.pt` | 多个 JSON 模型及 `manifest.json` |
| 流式处理 | 逐资源生成磁盘缓存，按窗口映射读取 | 逐资源读取、逐模型保存 |
| 断点续训 | 恢复权重、优化器、随机状态和批次进度 | 跳过已完成且数据、参数、文件均有效的模型 |
| 后续独立预测 | 当前没有独立的 LSTM predict CLI | `training.prophet predict`，不调用 fit |

LSTM 的“统一”是共享参数，不是把 CPU/内存或 request/limit 求和、平均。每条序列单独归一化，输出保留原指标名称。
Prophet 不能把某个容器训练的模型直接当作所有容器的通用模型。

验证准确性的推荐路线：**固定数据快照 → 时间切分 → 训练 Prophet → 训练 LSTM 并复用 Prophet 对照 → 检查覆盖率与配对误差 → 确认是否值得继续。**

## 2. 准备环境与数据

在新版源码的根目录操作，使用已经安装所需依赖的 Python 环境；如果它在 `.venv` 中，先执行 `source .venv/bin/activate`。

```bash
python --version
python -c "import torch, prophet, numpy, pandas; print('torch:', torch.__version__); print('prophet:', prophet.__version__)"
python -m training.lstm --help
python -m training.prophet train --help
```

已有包无需重装。缺包时按 [内网部署说明](../training/DEPLOY.md) 准备 CentOS 7 兼容依赖，下载使用阿里云镜像 `https://mirrors.aliyun.com/pypi/simple/`，不要直接升级到不支持 glibc 2.17 的最新组合。

### 2.1 固定一份已有数据快照

输入必须包含 `raw_index.json` 及其引用的 `raw/` 分片。不是 `items.json`、图表、预测报告或模型文件。
优先使用已有离线快照；若复制正在使用的数据目录，应在无写入的时段完整复制索引与分片。复制已有观测不等于重新拉取数据。

不要让采集程序在长时间训练期间改变同一个实验快照。LSTM 续训要求 raw 索引和缓存校验通过；不要删除 `data-cache/`。

下面的变量在同一个 bash 会话中设置。替换成内网实际路径，实验输出目录的子目录首次使用时必须不存在：

```bash
export RAW_DIR=/opt/resource_predict/snapshots/k8s-15days
export EXP_DIR=/opt/lstm-training/outputs/experiments/k8s-run-01
mkdir -p "$EXP_DIR"
ls "$RAW_DIR/raw_index.json"
ls "$RAW_DIR/raw"
```

如无需另做快照，也可把 RAW_DIR 指向现有 `outputs/k8s`，前提是实验期间保持该目录稳定。

### 2.2 核对数据时间和容器指标

以下仅读取索引和前三个资源，核对时间、点数、采样间隔。它是格式抽查，不是全量质量证明：

```bash
python - <<'PY'
import os
from pathlib import Path
from resource_predict.data.raw_store import RawResourceStore

store = RawResourceStore(Path(os.environ["RAW_DIR"]), max_cache_items=1)
ids = sorted(store.resource_ids())
print("索引资源数:", len(ids))
for rid in ids[:3]:
    item = store.get(rid)
    for container, metrics in item.get("container_metrics", {}).items():
        for name, series in metrics.items():
            print(rid, container, name, "点数", len(series),
                  "开始", series.index[0], "结束", series.index[-1],
                  "首个间隔", series.index[1] - series.index[0] if len(series) > 1 else None)
PY
```

若为 10 分钟间隔，15 天完整历史约 2160 点。训练时还会检查连续性和有效值；缺口不会自动跨越插值。
数据量按“Workload × 容器数 × 指标数”计算：3000 个 Workload、平均 2 个容器、4 个指标，约 24000 条序列。

## 3. 固定训练、验证、测试时间段

不能随机打乱原始时间点后再切分，否则可能把未来信息泄漏到训练中。所有资源共用相同的时间边界。

15 天数据可先按 **11 天训练、2 天验证、2 天测试**组织。下面只是示例日期，必须按实际快照调整：

| 时间段 | 示例区间，右端不含 | 用途 |
| --- | --- | --- |
| 训练 | 2026-08-29 00:00 ～ 2026-09-09 00:00 | 拟合 LSTM/Prophet；计算 LSTM 归一化参数 |
| 验证 | 2026-09-09 00:00 ～ 2026-09-11 00:00 | LSTM 早停和最佳 epoch 选择 |
| 测试 | 2026-09-11 00:00 ～ 2026-09-13 00:00 | 最终比较，不能据此继续挑参数 |

```bash
export TRAIN_END=2026-09-09T00:00:00
export VALIDATION_END=2026-09-11T00:00:00
```

时间与 raw 的时间基准保持一致，不擅自加减 8 小时。代码的截止时间均为“不含该时刻”。
当前测试结束时间取快照中可用数据末尾，没有 `--test-end` 参数；如果实际快照超过示例日期，测试区间会相应变长。
不显式指定边界时，默认最后 1 天测试、再前 1 天验证；为保证 Prophet/LSTM 对齐，本文使用显式边界。

15 天只覆盖约两周，11 天训练段甚至不足两个完整周周期。这足够开展首轮实验，但不能证明模型在月末、节假日、发布变更等场景长期有效。

## 4. 首次小规模检查

先取 4 个 Workload，训练 LSTM 2 轮，确认环境、缓存、报告均可正常生成：

```bash
python -u -m training.lstm \
  --raw "$RAW_DIR" --out "$EXP_DIR/smoke-lstm" \
  --resource-type k8s_workload --level container \
  --max-resources 4 --epochs 2 --checkpoint-every 10 \
  --train-end "$TRAIN_END" --validation-end "$VALIDATION_END" \
  --baselines seasonal_naive rolling_mean
```

小试跑只证明流程可运行。正式 LSTM 全量训练需另建目录，不能通过改变 `--max-resources` 在同一 LSTM 检查点上继续。

## 5. 正式训练 Prophet，冻结为对照模型

```bash
python -u -m training.prophet train \
  --raw "$RAW_DIR" --out "$EXP_DIR/prophet-baseline" \
  --resource-type k8s_workload --level container \
  --max-resources 0 --seed 42 \
  --train-end "$TRAIN_END"
```

- `--max-resources 0` 表示全部 Workload，不是只训练 100 个。
- 不指定 `--metric`，默认训练全部四类指标。
- 必须带 `--train-end`。如果先用完整 15 天训练 Prophet，再让它预测其中最后两天，会产生测试泄漏。
- Prophet 使用当前内部固定参数，暂不自动搜索超参数。验证段不会被加入这批冻结模型的拟合。
- 查看 `manifest.json`：`status` 应为 `complete`；再检查 `models`、`failures` 和 `skipped`。

训练失败不等于整个目录不可用，但正式比较前需要明确哪些序列未覆盖。当前 LSTM 加载冻结 Prophet 时，若缺少对应模型会在训练前拒绝继续，避免静默丢失对照。

### Prophet 中断恢复

同一命令追加 `--resume`，保持相同输出目录：

```bash
python -u -m training.prophet train \
  --raw "$RAW_DIR" --out "$EXP_DIR/prophet-baseline" \
  --resource-type k8s_workload --level container \
  --max-resources 0 --seed 42 --train-end "$TRAIN_END" --resume
```

`trained` 是本次新拟合数，`reused` 是有效检查点复用数，`available` 是本次总可用模型数。已训练完成后再次续训，正常应看到 `trained=0`。
源目录、模型参数、截止时间或版本变化时不能混用同一续训任务。详见 [Prophet 说明](../training/PROPHET.md)。

## 6. 正式训练 LSTM，并比较四种预测方法

```bash
python -u -m training.lstm \
  --raw "$RAW_DIR" --out "$EXP_DIR/lstm-all" \
  --resource-type k8s_workload --level container \
  --max-resources 0 --seed 42 \
  --lookback 24h --horizon 24h --stride 6 \
  --train-end "$TRAIN_END" --validation-end "$VALIDATION_END" \
  --hidden-size 32 --layers 1 --dropout 0.1 \
  --learning-rate 0.001 --batch-size 64 --epochs 20 --patience 3 \
  --threads 2 --checkpoint-every 1000 \
  --baselines seasonal_naive rolling_mean \
  --prophet-models "$EXP_DIR/prophet-baseline"
```

这里得到的对照方法是 `lstm`、`prophet_saved`、`seasonal_naive`、`rolling_mean`。
`--prophet-models` 自动加入冻结 Prophet 基线，不会在每个测试窗口重新拟合 Prophet。
若仅写 `--baselines prophet` 而不指定保存模型目录，则仍是每个测试窗口即时拟合，属于另一种实验口径。

主要参数说明：

| 参数 | 含义 |
| --- | --- |
| lookback=24h | 每个样本使用此前 24 小时观测 |
| horizon=24h | 一次预测未来 24 小时；10 分钟采样对应 144 点 |
| stride=6 | 训练窗口起点相隔 6 个点，10 分钟采样时为 1 小时 |
| epochs=20 | 最多训练 20 轮，不表示必然收敛 |
| patience=3 | 连续 3 轮验证误差未改善时停止 |
| checkpoint-every=1000 | 每 1000 个训练批次保存续训状态，轮次边界也保存 |

测试窗口不重叠，并按时间推进：例如两天测试会产生两次 24 小时预测。第二次的 LSTM 输入可以使用第一天已经到达的真实观测，但网络权重和归一化参数不更新。
冻结 Prophet 只按新时间起点外推，不吸收这些新值；轻量基线会使用各起点前的观测。这是“固定模型、观测随时间到达”的评估协议，不代表各模型都重新训练。

### LSTM 中断恢复

重跑第 6 节的完整命令，末尾追加 `--resume`，输出目录仍为 `$EXP_DIR/lstm-all`。
恢复需要整个目录中的 `run.json`、`resume.pt` 和 `data-cache/`；仅有最佳模型 `model.pt` 不够。
批量大小、窗口、模型结构、资源范围、种子、设备等关键参数必须一致。恢复机制和内存边界见 [LSTM 续训说明](../training/LSTM_RESUME.md)。

## 7. 第一层验证：训练和评价是否有效

先查看完成状态、数据覆盖、时间边界和训练曲线，暂时不要仅凭一个 RMSE 判断成功：

```bash
python - <<'PY'
import json, os
from pathlib import Path

base = Path(os.environ["EXP_DIR"])
status = json.loads((base / "lstm-all/status.json").read_text(encoding="utf-8"))
report = json.loads((base / "lstm-all/report.json").read_text(encoding="utf-8"))
manifest = json.loads((base / "prophet-baseline/manifest.json").read_text(encoding="utf-8"))
print("LSTM 状态:", status)
print("Prophet 状态/模型数/失败/跳过:", manifest.get("status"), len(manifest["models"]),
      len(manifest["failures"]), len(manifest["skipped"]))
for key in ("series", "series_by_metric", "window_counts", "boundaries", "best_epoch", "training_seconds"):
    print(key, report[key])
print("LSTM 跳过数:", len(report["skipped"]))
print("最近5轮:", json.dumps(report["history"][-5:], ensure_ascii=False, indent=2))
PY
```

判断方法：

- `status=complete` 才表示本轮报告生成完成；`running/interrupted` 时先恢复任务。
- 核对是否覆盖预期 Workload、容器、指标和时间段。大量跳过时，不能把剩下的小部分当作全量效果。
- 训练误差下降、验证误差上升，提示可能过拟合；应使用最佳验证 epoch，而不是最后一轮权重。
- 最佳 epoch 一直等于训练上限，且验证误差仍下降，只能说可能需要更长训练，不能宣称已经收敛。
- 训练与验证误差都是逐序列标准化后的 MSE，不是原始比例尺度的最终预测误差。

## 8. 第二层验证：独立测试是否优于基线

### 8.1 看哪些指标

记真实值为 y、预测值为 yhat，误差 e = yhat - y：

| 指标 | 项目计算含义 | 用途及注意事项 |
| --- | --- | --- |
| RMSE | sqrt(mean(e²)) | 衡量总体误差，对大误差更敏感 |
| MAE | mean(abs(e)) | 比较平均偏差，较易解释 |
| MAPE | mean(abs(e) / max(abs(y), 1e-9)) | 报告保存比例值；真实值接近零时会非常大，不宜作为唯一标准 |
| P95 error | 每个窗口 abs(e) 的第 95 百分位 | 观察尾部误差，不是“预测准确率 95%” |
| mean_underprediction | mean(max(y-yhat, 0)) | 平均低估程度，越低通常越有利于容量保护 |
| peak_underprediction | max(max(y)-max(yhat), 0) | 窗口最大值的低估；不反映峰值是否发生在正确时刻 |

若数据确实是使用率比例，MAE=0.03 表示平均偏差约 3 个百分点。request 使用率可以超过 1，不要把它强行理解成最大 100%。
项目 raw 的某些指标在缺少规格时可能使用核数/GiB 等回退口径，需核对 `container_metric_modes`；不同单位不能合并解释成“百分点”。当前 `summary_by_metric` 按名称分组，并不自动按这些单位分组。

没有通用的“RMSE 小于某值就一定准确”。应同时满足预先确定的业务误差容忍范围，并与同数据、同窗口的轻量基线比较。

### 8.2 快速查看分指标报告

```bash
python - <<'PY'
import json, os
from pathlib import Path

r = json.loads((Path(os.environ["EXP_DIR"]) / "lstm-all/report.json").read_text(encoding="utf-8"))
for metric, methods in r["summary_by_metric"].items():
    print("\n指标:", metric)
    for name, scores in methods.items():
        print(name, "成功/失败窗口", scores["successful_windows"], scores["failed_windows"],
              "RMSE", scores["mean_window_rmse"], "MAE", scores["mean_window_mae"],
              "P95误差", scores["mean_window_p95_error"],
              "峰值低估", scores["mean_window_peak_underprediction"])
PY
```

不要用跨四类指标的总 `summary` 代替分指标结论。该汇总是“每个窗口先计算误差，再等权平均”，不是把所有预测点合并后计算的整体 RMSE/P95。
模型失败窗口数不同时，直接比较各自成功窗口的均值并不公平，应只比较共同成功的窗口。

### 8.3 对齐共同成功窗口后比较

以下脚本读取 `forecast_error_report.json`，按资源、容器、指标、起止时间配对。它会把明细放入内存，大型报告宜在有足够内存的分析节点运行。

```bash
python - <<'PY'
import json, os
from collections import defaultdict
from pathlib import Path
from statistics import mean

path = Path(os.environ["EXP_DIR"]) / "lstm-all/forecast_error_report.json"
records = json.loads(path.read_text(encoding="utf-8"))["records"]
models = ("lstm", "prophet_saved", "seasonal_naive", "rolling_mean")
windows = defaultdict(dict)
for row in records:
    if row["model"] in models:
        key = tuple(row[k] for k in ("resource_id", "container", "metric", "window_start", "window_end"))
        windows[key][row["model"]] = row
for metric in sorted({key[2] for key in windows}):
    candidates = [scores for key, scores in windows.items() if key[2] == metric]
    paired = [scores for scores in candidates
              if all(scores.get(model, {}).get("status") == "ok" for model in models)]
    print("\n", metric, "共同成功/记录窗口:", len(paired), "/", len(candidates))
    if not paired:
        print("没有共同成功窗口，不能比较")
        continue
    reference = mean(scores["seasonal_naive"]["rmse"] for scores in paired)
    for model in models:
        rmse = mean(scores[model]["rmse"] for scores in paired)
        improvement = (reference-rmse)/reference*100 if reference > 1e-12 else None
        print(model, "RMSE", round(rmse, 6),
              "相对Seasonal Naive改善%", round(improvement, 2) if improvement is not None else "基线接近零，不计算百分比",
              "MAE", round(mean(scores[model]["mae"] for scores in paired), 6),
              "P95误差", round(mean(scores[model]["p95_error"] for scores in paired), 6))
PY
```

改善百分比为 `(基线 RMSE - 模型 RMSE) / 基线 RMSE × 100%`。正数是改善，负数是退步；不是“预测准确率”。
共同成功窗口之外，还要报告未配对、失败和训练阶段跳过的数量，不能只展示成功子集。

### 8.4 还要检查哪些现象

1. **逐资源差异**：平均误差改善是否只由少量高负载资源贡献？可以先按资源—容器汇总，再统计相对固定基线的胜率，不能把大量重叠训练窗口当作独立证据。
2. **低估风险**：是否在高负载时持续低估？即使 RMSE 更小，也可能不适合容量规划。
3. **峰值时间**：检查 `predictions.jsonl` 的真实值和曲线是否对齐；窗口峰值高度接近，但时间错位仍可能导致扩容不及时。
4. **稳定性**：工作日和周末表现是否一致？15 天初测不足以覆盖所有季节规律。
5. **成本**：比较训练耗时、加载预测耗时、失败率和内存。当前计时范围不同，不要将 LSTM 共享训练总时间与一个 Prophet 模型时间直接相比。

`predictions.jsonl` 的窗口时间戳按 `window_start + 点序号 × report.boundaries.step_seconds` 还原，真实值在 actual，模型曲线在 predictions。

## 9. 怎样决定继续调参还是保留基线

| 观察结果 | 下一步 |
| --- | --- |
| 大量序列跳过/模型失败 | 先排查数据覆盖、缺口、时间边界、依赖，不据此评价算法优劣 |
| 训练和验证都很差 | 核对归一化、输入长度和训练预算，再做少量候选实验 |
| 训练下降、验证恶化 | 使用早停最佳轮次，比较更小网络或更强正则化 |
| 验证到上限仍改善 | 可提高 epochs 上限并续训；不要用测试误差决定何时停止 |
| LSTM 没超过 Seasonal Naive/Rolling Mean | 保留轻量基线，没有必要仅因模型复杂就替换 |
| Prophet/LSTM 只在部分指标获益 | 分指标、分资源类型选择，不宣称所有指标全面提升 |
| 误差改善但峰值低估明显增加 | 先核对容量风险容忍范围，不仅看平均 RMSE |

LSTM 可先比较少量 lookback、hidden-size 和学习率候选，每个候选使用新的输出目录，并只依据验证曲线选择。改变这些参数不能在同一检查点上续训。
当前脚本每次都会生成测试报告，没有“调参时完全不执行测试”的开关；调参阶段不要用这些测试分数选择候选。一旦反复据其修改方案，这段测试只能算开发证据，需要保留新的独立时间段做最终确认。
Prophet 当前无 CLI 自动调参流程，先将默认参数作为冻结对照。不能把模型能序列化或运行测试通过，称为业务预测精度已验证。

## 10. 验证通过后，怎样用于真正的未来预测

### 10.1 Prophet 训练发布版本

实验对照模型刻意没有看验证/测试数据；确认方案后，可以另建目录，用全部已有历史训练面向未来的版本：

```bash
python -u -m training.prophet train \
  --raw "$RAW_DIR" --out "$EXP_DIR/prophet-release" \
  --resource-type k8s_workload --level container --max-resources 0 --seed 42

python -u -m training.prophet predict \
  --models "$EXP_DIR/prophet-release" \
  --raw /opt/resource_predict/outputs/k8s \
  --out "$EXP_DIR/prophet-future-01" --horizon 24h
```

predict 只加载模型，raw 用于确定每条指标最新时间起点。新数据不会自动更新趋势参数；长期复用需要按误差和业务变化决定何时显式重训。
对当前未来预测，真实值尚未到达，因此没有可立即计算的准确率。之后利用正常采集已落盘的实际观测，按资源—容器—指标—毫秒时间戳与原预测配对，再计算同样的误差指标。
不能等真实值出现后重新训练，再把新模型对这段历史的拟合结果当作原预测误差。
当前独立 Prophet predict 不自动补评未来误差；不要把 report.json 的 fit_calls=0 当作精度证明。

### 10.2 LSTM 当前交付边界

`model.pt` 保存最佳验证轮次的模型及每条序列的归一化参数，现在可按 [在线接入说明](lstm-online.md) 配置为原系统候选。当前仍没有独立 LSTM predict CLI，也没有自动“用全部 15 天重训发布模型”的模式。
不要把再次运行 `training.lstm` 当作只加载预测——它仍执行训练/恢复及测试流程。
训练产物不会自动替换原系统模型或触发扩缩容。启用在线 LSTM 候选后仍须经过验证选型和既有执行门槛；先检查真实误差及失败原因再扩大范围。

## 11. 实验留档清单

- 固定 raw 快照、实际起止时间、采样间隔、资源和容器覆盖数量。
- 完整命令、软件版本、硬件配置、seed、训练/验证/测试截止时间。
- Prophet 的模型目录、manifest、run.json 和 checkpoints。
- LSTM 的 model.pt、resume.pt、run.json、data-cache、status.json。
- report.json、forecast_error_report.json、predictions.jsonl。
- 共同成功窗口的配对比较、失败/跳过计数、分指标与关键资源结果。
- 明确标注哪些日期用于调参，哪些用于独立测试；尚未验证的场景和上线限制。

模型、原始数据和含资源标识的明细可以全部留在内网。汇报时可提供经内部允许的脱敏汇总，不需要把完整监控数据传到外网。
