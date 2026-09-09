# 内网离线实验操作说明

## 直接查看预测误差与计算成本（results 版）

使用 routing-experiment-results.zip 解压后的 routing-experiment 文件夹，复用已有明细：

```bash
python -X utf8 -m benchmarks.routing_results --run-dir /data/pilot-k8s-new --output share-routing-results.json
```

不重新训练 Prophet，不再分别运行 feedback/policy-suite。仅回传内部审核通过的 share-routing-results.json。
同时给出四个策略的每指标平均窗口 RMSE、MAE，与快速基线和全部增强流程比较的误差增幅，
以及含快速基线和影子追加成本的完整工作流阶段估算。正的 error increase 表示误差更差，零基准输出 null。
这些是窗口 RMSE 的算术平均，不是所有采样点合并后的 RMSE。CPU/memory、request/limit 分开，不混合原始误差。
不暂停 ungated 仍是受预算约束的路由，不等于 all_enhanced；后者是在每条序列均追加 Prophet 候选再验证选型。
没有完整实测耗时，measured_end_to_end_cost_seconds 为 null。成本估算不包含路由训练、特征生成、I/O 等开销。
部分旧记录缺评分则标为 partial，不得把这部分成本节省外推全批。预算超支指标仍针对边际预算。
本次仅补评分口径，不修改策略，不自动设定误差可退化阈值，不产生新的独立验证证据。

## 收敛后的四策略集中对照（policy-suite 版）

使用 `routing-experiment-policy-suite.zip` 解压后的 `routing-experiment` 文件夹。
复用此前花一小时生成的 **pilot-k8s-new**，无需再运行 routing_pilot 或 Prophet：

```bash
python -X utf8 -m benchmarks.routing_policy_suite --run-dir /data/pilot-k8s-new --output share-routing-policy-suite.json
```

替换为真实内网目录，该目录须同时包含 pairs.jsonl 和 report.json。仅返回内部审核通过的
share-routing-policy-suite.json；无需再发旧 feedback、gate、calibration 报告。
可以一次给 --run-dir 列多个目录，仍只生成一个文件。

固定比较四种策略：不暂停、原提前成本反馈门控、额度不足标记人工复核、有界延后探测。
每种均对照 0/4/24 小时收益反馈延迟，共享冻结训练、Q90 成本及每批 50% 名义预算。
新两种策略共同采用平均负收益幅度 0.001、累计损失 0.001、探测额度下限、初始暂停期额度 0.6 倍。
延后方案每次最多加 0.2 倍、最多两次、至少隔 6 个可评分批次；两者差异仅为追加授权。
相对原规则则同时变更了触发条件和探测下限，不可把全部收益变化都归因于追加授权。

policy_overview 直接给出各策略收益、总开销、影子开销、超支、暂停、恢复次数、
人工复核标记次数与加额次数，完整事件记录一并保留，通常无需补传文件。
没有任何自动发送通知、真实任务执行或生产配置变更；这仍是旧数据上的开发比较。
应同时检查收益和实际花费，不能因为恢复次数变多就选更昂贵的策略。
默认策略未改为新规则，只有本入口显式启用新候选。

本地 `python -X utf8 -m benchmarks.routing_exhaustion --output exhaustion.json`
对比“保持暂停并标记人工复核”和“最多两次有界延后探测”；不要求内网运行或回传。
延后探测额外消耗资源且不保证恢复，原有 CLI 未默认启用此策略。

本地可选 `python -X utf8 -m benchmarks.routing_gate_limits --output gate-limits.json`
用于累计负收益和暂停期探测额度的开发消融；不要求内网运行，不改变已有命令的默认规则。
观察到额度耗尽可能导致长期暂停，因此目前不是可部署的自动恢复方案。

本地开发对照新增 `benchmarks.routing_gate_options`：
`python -X utf8 -m benchmarks.routing_gate_options --output gate-options.json`。
只运行人工候选场景，不调用 Prophet；目前无需在内网运行、不要求回传。
幅度门槛与探测额度下限没有成为现有回放命令的默认设置，不能将本地结果当作新数据验证。

## 成本与收益反馈分离（一次敏感性对照）

feedback 版复用明细，不重新运行 Prophet；比较不暂停、成本/收益耦合反馈、成本下一批交付三种策略：

```bash
python -X utf8 -m benchmarks.routing_feedback --run-dir /data/pilot-k8s-round2 --output share-routing-feedback.json
```

只需返回内部审核通过的单个汇总；支持多个 run-dir。无需同时运行之前的 gate 命令。
如要减少搬运频次，可留待下次集中操作；不需重新采集原始数据。
三个延迟 0/4/24 小时仅从评估开始作用于收益反馈，历史热启动统一按额外延迟 0 小时选择训练数据，
所以三种延迟、三种策略共享模型、预算与初始选择。不能解读为真实历史始终存在 24 小时延迟。

成本下一批交付是显式假设：上一批所有已选计算在下一可评分批次决策前完成。
没有真实任务完成时间戳，不声称此时序真实成立，也不会在当前任务中途停止它。
正常成本反馈不清除负收益连续次数，不恢复暂停；恢复仍需已选影子样本的完整收益反馈。
超过成本阈值的影子结果会清除健康恢复计数；旧 epoch 反馈不会改变新状态。

batch_diagnostics 导出候选数量及相邻特征截止点间隔（小时），无绝对日期、资源或批次身份。
这些间隔只能帮助核对时间分组，不能自动证明真实调度批次一致，禁止按汇总盲目合并。
全失败时刻仍为不可评分，反馈递送按下一可评分批次，不能评估失败期间恢复耗时。

## 已选反馈驱动的暂停回放（研究原型）

新增 `routing_gate_replay`，同一批明细分析 0/4/24 小时额外反馈延迟，一次输出一个汇总：

```bash
python -X utf8 -m benchmarks.routing_gate_replay --run-dir /data/pilot-k8s-round2 --output share-routing-gate.json
```

不要求现在搬包或运行；可与后续新批次集中处理。模型使用前 70% 起点边界之前已知历史拟合，
内部校准后冻结；后续只有已选任务的到达反馈影响门控，不用未选任务更新模型或恢复状态。
改变整份数据的起点数量会改变 70% 边界，因此此命令是开发划分，不是固定日期的前瞻确认。
正式确认需预先固定边界，当前 API start_origin 支持本地检查但 CLI 不承担确认协议。

正式策略与无门控策略共享冻结模型、每批 50% 名义预算和 Q90 成本；两者结果分别计分。
暂停时每三次可评分批次，随机抽一个正预测收益、且预测成本不超过本批预算 10% 的候选。
若无可负担样本，明确 no_affordable_probe，可能持续暂停，绝不临时提高预算。
影子成本计入总量，影子收益不记为正式收益。恢复只依据两次影子反馈，代表性未经证明。

该入口仍是全候选成功的条件回放，不将历史失败视作新策略能处理的失败；
全失败时刻标记为 unscorable_origin_numbers，status=partial，不能称完整闭环通过。
门控批号按可评分批次计数，不能用部分结果测量失败期间的实际恢复时间。
默认反馈可用时刻为选中候选最晚 test_end 加额外延迟，在后续 feature_end 截止点之前才交付。
这些是保守代理，不是真实监控到达时间；当前尚不包含硬超时、真实反馈持久化或生产执行。

本地可选机制测试：`python -X utf8 -m benchmarks.routing_pause --output pause-simulation.json`。
这是人工指定结果的暂停/恢复模拟器，不读取 raw、不运行 Prophet、不控制在线任务；
目前不要求在内网运行或回传，不能用模拟收益作为真实收益证明。

## 审计修正版：逐预测时刻评价

压力修正版 routing-batch-v2 会保留全部候选失败的已知时刻，并报告无起点记录数量。
可选本地 `python -X utf8 -m benchmarks.routing_stress --output stress.json` 运行五类固定记录场景；
不拟合 Prophet，不需要内网数据，不要求用户为此回传文件。

batch 版按精确 origin_time 分批；每批仅使用 test_end 早于该批最早 feature_end 的历史标签，
并独立拟合/校准、独立预算，不跨时刻借用预算。旧跨时段分析不覆盖，继续作为静态池对照。
无需原始数据或重新拟合 Prophet，可复用已有明细：

```bash
python -X utf8 -m benchmarks.routing_batch --run-dir /data/pilot-k8s-round2 --output share-routing-batch.json
```

支持 --run-dir 后列多个目录，只输出一个经内部审核后可带回的汇总文件。
早期批次因嵌套历史不足会保留 insufficient_data；若精确时间不对齐导致单批太小，
不得事后合并成大批美化结果，应核实内网实际调度批次。
按全候选成功样本分析，排除数量保留；尚不支持失败情况下的真实决策回退。
标签可用时间仍以 test_end 代理，未建模真实采集迟到；成本仍为阶段估算，不是 CPU 或生产端到端成本。

另提供可选本地合成计时核验（目前无需在内网运行）：

```bash
python -X utf8 -m benchmarks.routing_timing --repeats 3 --output timing-check.json
```

分别独立执行两套完整研究工作流，并与阶段合成估计比较；三种计时操作循环换序。
共享解释器，不声称隔离冷启动；不等同于当前生产管线全候选图表输出。

成本校准开发实验完成后，按 [新时间段确认协议](routing-confirmation.md) 固定方案并评价新观测。
继续使用内网已有包即可，不必因协议更新搬运新代码。

## 一次完成成本校准与预算验证（calibration 综合版）

本版把成本校准、所有固定预算档、策略对照和重复留出汇总放进同一条命令。
只需要内网已有 `pairs.jsonl` 和 `report.json`，无需重新跑 Prophet：

```bash
python -X utf8 -m benchmarks.routing_calibration --run-dir /data/pilot-k8s-round2 --output share-routing-calibration.json
```

若已积累 VM 或另一批快照，可一次处理多个目录，仍只导出一个文件：

```bash
python -X utf8 -m benchmarks.routing_calibration --run-dir /data/pilot-k8s-round2 /data/pilot-vm-round2 --output share-routing-calibration.json
```

只带回审核通过的 `share-routing-calibration.json`，并用文字说明 run 1/run 2 各对应哪类资源即可。
汇总不包含目录名、资源标识或精确日期；各批结果独立，不能将重叠快照当作新增独立样本。
命令会按批次打印进度。尚未积累第二批时直接使用单目录命令，不必为了凑齐而重新运行模型。

### 固定实验矩阵

- 保留 10 个资源划分、3 个滚动时间区间与 10%/25%/50%/75% 名义预算。
- 五种成本：常数中位数、常数均值、原始 log Ridge、均值比例校准、保守 Q90 比例校准。
- 每次外层训练的最后 30% 预测起点作为校准段；拟合标签必须早于校准段最早特征截止时间。
- 所有成本方案使用同一拟合子集。收益模型继续使用完整外层训练集，不变更收益算法。
- 均值系数为校准真实成本之和/校准预测成本之和；保守系数取 1、均值系数、逐条真实/预测比值 Q90 的最大值。
- **所有方案共用拟合子集平均成本 × 测试条数 × 档位的预算**，校准不能同步增大预算。
- 四种分配策略与 budget 版相同，随机各运行 20 次；原始和校准方案都用预测成本决定准入。
- 实际成本只用于事后评分。原始负差值按 max(delta,0) 核算并保留数量，不能解释为真实负算力。
- Q90 是经验逐任务比值分位数，不保证整个批次 90% 不超支；没有宣称概率保证。

### 自动汇总与样本不足

`overview` 对每种成本、预算和策略同时汇总收益、超支率、超支幅度、预算利用率，
以及相对两个随机基线的胜出次数；各次明细仍保留，通常不需要再导出补充分析文件。
随机策略的 `overrun_rate` 是 20 次随机顺序中的超支比例；确定性策略为 0 或 1。
跨划分分布是描述性统计，不是独立实验置信区间。降低超支但几乎不选任务，不等于更好的路由。

内层至少 5 个预测起点、30 条拟合记录、10 条校准记录，外层至少 10 条测试记录。
嵌套时间隔离会使早期时间区间不足，输出 `nested_fit_or_calibration_history_insufficient` 是正常结果。
不要放松时间隔离来凑样本；以后有更多历史再运行。报告不会根据测试结果自动挑选最优方案。

已反复分析的第二轮仍是开发数据。你可以先把本次综合汇总发回；后续只在积累了新的完整时间批次后
再发送一次汇总，而不需要每个策略、每个预算分别传文件。最终新时间段应在方法固定后评估。

## 同预算开发实验（budget 版，复用第二轮明细）

解压带 budget 后缀的新包，运行：

```bash
python -X utf8 -m benchmarks.routing_budget --run-dir /data/pilot-k8s-round2 --output share-k8s-budget.json
```

只返回审核通过的 `share-k8s-budget.json`，无需原始 raw，不重新拟合 Prophet。
沿用 10 次资源留出和 3 个滚动时间区间。收益模型不变；追加耗时采用训练集标准化的
log Ridge(alpha=1)，预测值限制在训练成本范围内。测试真实耗时不进入预测、预算设定或选择。
预算为测试候选预测成本总和的 10%、25%、50%、75%，是各策略共同的名义追加预算。
分配扫描按顺序接纳可负担项目，跳过当前放不下的项目；不保证背包最优。

对比随机顺序、仅正预测收益候选中的随机顺序、按预测收益、按预测收益/预测成本。
两种收益策略仅选择正预测收益候选；随机各运行 20 个固定种子，用于描述随机选择波动。
统计实际开销、超预算量、未用预算和总归一化收益，而不是只看单位样本收益。
**相同名义预算不等于相同实际花费**，实际超预算的高收益不能直接视作策略更好。

原始 delta_wall_seconds 可能因重拟合差异/计时噪声为负。本实验用 max(delta,0) 作为非负
追加成本代理，训练取对数时下限 1e-6，报告负值条数，不按测试成本删除样本。
若负值较多，需要独立流程计时，当前预算结论不可直接用于生产。基础模型开销不在追加预算内。
另行报告收益/成本模型拟合与推理耗时，以及全套策略分析耗时；这是运行分析机器上的时间，
不包含原始特征提取开销，也未从候选预算扣除，后续在线测量需计入这些开销。

本批数据已经多次用于开发，不能作为最终确认集。随机试验分布不是业务泛化置信区间，
重复留出也不是独立实验；请开始保留后续未参与调参的新时间段。

## 已完成第二轮：直接复用明细分析（2026-09-08）

解压新版包，在其目录运行；run-dir 指向内网第二轮原有输出目录，必须同时包含
`pairs.jsonl` 和 `report.json`。本步骤不运行 Prophet，不需要原始 raw 文件：

```bash
python -X utf8 -m benchmarks.routing_validation --run-dir /data/pilot-k8s-round2 --output share-k8s-validation.json
```

只带回审核通过的 `share-k8s-validation.json`。
包含 10 次固定种子的资源分组留出，以及后半段时间中 3 个不重叠测试区间的滚动验证。
时间训练标签严格早于该测试区间最早特征截止时间。每次仍需至少 30 条训练和 10 条测试记录。
各次输出选择前 10%、25%、50%、75% 的归一化收益和追加墙钟成本，以及同条数随机选择的期望值。
追加成本可以为负（阶段复用计时的噪声或重拟合成本变化），不截断、不解释成负算力；
尚未做同预算在线调度。随机成本为全测试集追加成本乘以选择比例。

资源级 bootstrap 使用 500 次重采样；区间是固定拟合模型与固定选择下的
等资源权重收益优势区间，不是重新训练的泛化区间，也不是行加权平均收益的区间。
测试资源少于 5 个不输出区间。重复划分共享数据，不能作为独立重复合并计算显著性。
资源留出不是业务留出。所有比例是预先固定的诊断，不要挑表现最好的比例作为最终验证结果。

旧明细没有保存 auto 规则准入结果，输出 `unavailable_missing_archived_rule`；不会从不完整特征猜测。
新版回放会额外保存 `auto_rule_run`，对照为 enabled=true、mode=auto 的 Prophet 规则，
使用当时验证前历史和当前包默认异常阈值；不代表内网部署时用户自定义的运行模式。
以后新回放可自动产生规则对照，已有第二轮无须重跑。

本包包含第一阶段实验源码，**不包含第三方依赖 wheel 或 Python 解释器**。
优先使用内网现有项目的可运行 Python 环境；包内不含原始数据、部署配置和凭据。
实验只读取原始快照，不连接 Prometheus，不启动 Web 服务，不调用实际调配。

## 1. 环境准备

解压 ZIP，进入 `routing-experiment/`。本工具要求 Python 3.10 或更新版本。
项目模块依赖一起打包以保持现有预测实现一致，不复制整个 `.venv`。

已有项目环境时，使用该环境的 Python 执行后续命令。下面以 Linux 为例：

```bash
source /path/to/existing-project/.venv/bin/activate
python -X utf8 -c 'import numpy, pandas, statsmodels, prophet, flask; print("imports OK")'
python -X utf8 -m benchmarks.routing_pilot --output smoke-synthetic
python -X utf8 -m benchmarks.routing_share --run-dir smoke-synthetic --output smoke-share.json
```

若缺少依赖，在**与目标相同 OS/架构/Python 版本，兼容目标 glibc 的联网机器**准备：

```bash
python -m pip download --only-binary=:all: -r requirements.txt -d wheelhouse
```

将 wheelhouse 按内网规定导入，在内网安装：

```bash
python -m pip install --no-index --find-links wheelhouse -r requirements.txt
```

不要使用 Windows wheelhouse 安装到 CentOS。若无兼容 wheel，需在匹配系统准备构建产物；
当前包未验证你的 CentOS/Python 组合，不能承诺拿到源码 ZIP 后零准备启动。
requirements 沿用项目版本范围，实际版本会记录在内网 `report.json` 中。
Windows 使用 `.venv\Scripts\python.exe -X utf8`；UTF-8 模式避免 Prophet 输出解码失败。

## 2. 数据与预检

输入为现有系统 `outputs/vm` 或 `outputs/k8s` 下的 `raw_index.json` 和对应 `raw/` 分片。
请在内网制作固定只读副本，并保持两者一致。不能只复制索引，也不要让采集任务同时更新该副本。
其他格式需先在内网转换为项目 raw 格式，本包不自动访问监控系统。

```bash
python -X utf8 -m benchmarks.routing_share --check-raw /data/frozen/vm --limit 12 --origins 3 --horizon 24 --output preflight-vm.json
```

检查 eligible 数量、历史跨度、horizon_hours 和计划拟合次数。
`horizon` 是点数：1 小时粒度的 24 点是 24 小时；10 分钟粒度要用 144 点才能预测 24 小时。
首轮抽 12 个资源、3 个起点验证流程；每起点运行 3 模型 × 2 阶段，容器数会增加拟合次数。
预检不运行模型，不能估算准确耗时。缺失/不等间隔数据会跳过，不能将跳过的数据视为正常样本。

## 3. 回放与汇总

```bash
python -X utf8 -m benchmarks.routing_pilot --raw-dir /data/frozen/vm --limit 12 --origins 3 --horizon 24 --output pilot-vm
python -X utf8 -m benchmarks.routing_share --run-dir pilot-vm --output share-vm.json
```

K8S 同样操作，修改 raw 路径与输出名。所有输出目录/文件必须是新名称，不覆盖旧实验。
验证首轮耗时可接受后，增加到例如 30 个资源、14 个日预测起点，保持 horizon 对应相同实际时长。
至少需要 `(origins+1)*horizon+48` 点。建议真实数据覆盖 4～8 周。
回放没有单模型硬超时；先小样本运行，如单次拟合长时间卡住，停止进程并保留内网日志。
只有正常完成、有 `report.json` 且原始索引未变化的实验可以导出汇总。

## 4. 带回哪些文件

**仅带回内部审核通过的 `share-vm.json`、`share-k8s.json`，可附预检 JSON。**
导出器重新构造固定字段汇总，不包含资源/容器/业务名称、哈希、路径、精确时间戳、
原始异常文本、配置或曲线。不要带回 `pairs.jsonl`、`report.json`、控制台日志或 raw 文件。
汇总仍包含样本数和统计分布；这是数据最小化，不是差分隐私或任何保密认证，仍需内部审核。
请用文字注明：数据是真实还是模拟；VM/K8S；采样间隔；是否有多个业务；CPU/内存及并发运行情况。
不需要提供业务名称或内网地址。

## 5. 如何解读收益探针

资源留出按资源分组，避免同一 Workload 的多个容器分散到训练和测试；至少 10 个资源。
时间留出使用后 30% 起点，训练标签必须早于测试特征截止时间；至少 5 个起点。
两类验证均至少需要 30 条训练记录、10 条测试记录，否则 `insufficient_data` 是正常结果。
默认 3 起点的首轮不能做时间留出。

固定 Ridge(alpha=1) 的标准化参数仅从训练集计算，不调参；与训练集均值常数预测比较 MAE。
标签为 RMSE 改善除以 `max(abs(训练历史均值),0.01)`，仅适用于本项目比例型指标。
仅全候选成功配对进入探针，汇总保留全部失败数；探针结论是条件于拟合成功的结果。
top_half 与随机一半是**相同条数**对照，不是相同算力预算。资源留出不是业务留出；
没有跨业务分组和重复实验置信区间时，不据此宣称路由有效或直接进入自动执行。
