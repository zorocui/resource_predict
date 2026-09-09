# 万级资源多核预测实施计划

用户要求解决预测慢、服务器CPU空闲，并明确部署规模为约万个资源、虚拟机vCPU/内存可调。当前实现已有资源线程池，但容器指标顺序执行；本轮保持模型与评估算法，改为统一指标任务并行。

## 设计合同

- 新运行配置 prediction.parallel_backend=auto|process|thread|serial（默认auto），prediction.max_workers=0表示自动；显式正整数限制并发。auto对ARIMA/SARIMA/Prophet使用process，只有轻量模型时使用thread避免启动开销。CLI同名覆盖参数只作用本轮。
- 自动识别虚拟机可用vCPU，考虑Linux affinity/cgroup配额，每轮重新计算；自动保留1核（至少1）。Windows进程池最多61个。并发不超过任务数；max_workers=1使用串行执行。
- 任务为(resource_index,container,metric,series)，容器/聚合指标统一调度；一条任务执行该指标现有验证、测试及在线预测，不额外拟合。多进程统一spawn，顶层可pickle函数，适用于Windows及从Web后台线程启动。
- 子进程只接收单条时间序列和键，不复制整批资源；进程initializer只发送一次WorkerContext和冻结设置。最多2×workers个在途任务；完成一个补一个，避免万级任务队列膨胀。
- 每个子进程原生BLAS/OpenMP线程限制1，保留Prophet外部进程环境限制；使用已可用threadpoolctl并显式列为依赖。线程模式不在每个线程独立改动全局原生线程限制。
- SettingsProxy增加上下文冻结支持，当前批次的模型、决策、采集参数在父组装与子拟合一致，配置保存只影响下一轮。线程上下文显式传递，不能更改全局配置文件。
- worker支持从预计算结果组装，保持输出顺序、单项/容器图表、独立测试留档、置信度、规格与门控。进程失败必须可见，不静默切成线程或丢失部分资源。
- 日志与generation_stats.json保存实际backend/workers/可用CPU、任务数、峰值在途任务、进程数、拟合/留档与后处理/写盘耗时，区分模型耗时和CPU空闲的I/O阶段。
- 提供真实预测基准脚本对比serial/thread/process，不基于45资源mock宣称万资源吞吐；报告包含启动开销、worker PID与输出数值一致性。本机结果只说明本机，部署机需按其模型/序列长度实测。

## 步骤

- [x] 1. 配置、冻结设置上下文、系统配置页面和CLI覆盖。
- [x] 2. worker拆出统一指标输入与预计算组装，覆盖K8S容器与部分重算。
- [x] 3. spawn多进程、有界提交、CPU配额识别和运行统计；接入generate_forecasts。
- [x] 4. 真实多进程一致性/错误传播/冻结配置/进程ID验证、基准与文档；全量Python四项检查及Node/页面检查。

## 完成记录

全量683 passed、28 subtests passed，保留既有NumPy非有限输入警告；compileall、pyflakes、vulture --min-confidence 80（含benchmarks）、git diff --check通过。Node35项及JS语法检查通过。实际页面显示自动/多进程/多线程/串行和最大并行数0，未保存或改动用户部署配置。

真实ARIMA/Rolling Mean跨serial/thread/spawn测试确认两个子进程、数值/误差/选择模型/config_hash一致。单Workload12个聚合/容器任务通过完整进程管线，保持图表和独立测试留档。独立复核补上失败后等待已开始任务退出，以及单独记录父进程assembly_seconds，避免被当作纯模型计算。

本机ARIMA基准8任务×240点：serial15.7358秒、process2为10.6207秒（含启动/IPC），约1.4816×，两个真实子PID，无模型失败、数值一致；结果位于outputs/benchmarks/multicore-smoke.json。不作为万级生产吞吐保证。

保留既有评分、调配成效、准确性及routing修改，未提交/推送。测试缓存沿用此前自动审批删除限制，未使用替代删除方式绕过。
