# 多核并行预测与吞吐验证

预测调度从“资源线程池、容器顺序计算”改为统一的资源/容器指标任务池。每个任务执行该指标原有的模型验证、独立测试和未来预测；父进程组装图表、建议、容器目标与留档。模型参数、选模规则、误差和执行门控不因并行方式改变。

## 启用方式

更新依赖并重启服务，进入“系统配置 → 预测配置”：

| 字段 | 默认值 | 行为 |
| --- | --- | --- |
| 预测并行后端 parallel_backend | auto | ARIMA/SARIMA/Prophet使用多进程；仅Seasonal Naive/Rolling Mean时使用线程，减少启动开销 |
| 最大并行数 max_workers | 0 | 自动识别可用CPU并保留1核，至少1；显式1为串行，2–256限制并发数 |

也可选择process、thread或serial。实际并发取配置值、可用CPU数、任务数的上限交集；Windows进程池最多61个。识别逻辑vCPU、CPU affinity与Linux cgroup配额，不只读取宿主机核数。虚拟机调整vCPU后，每轮按操作系统实际可用值重新规划。设置保存只影响下一批，当前批次的模型、决策和采集设置通过不可变上下文冻结。

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt

# 重用已有raw数据，显式使用8个进程
python generate_forecasts.py predict --parallel-backend process --max-workers 8

# 按当前虚拟机资源自动规划，不修改页面配置
python generate_forecasts.py predict --parallel-backend auto --max-workers 0
```

predict按已有VM/K8S输出目录分别重算，不重新采集raw。Web后台预测同样使用页面配置。CLI没有新参数时沿用页面设置；环境不支持创建进程时明确报错，不静默退化为线程。

## 万级资源的并发与内存

每个VM通常有3个任务；K8S每个Workload有4个聚合指标任务，再加每个有效容器4个任务。部分重算、短序列跳过会改变数量，以日志为准。即使只有一个多容器Workload，也能分摊到多个核心。

子进程只接收当前指标序列和标识，WorkerContext与冻结Settings初始化时各传入一次，不把全部万个资源重复序列化。最多2×workers个任务在途，完成后继续补充。父进程仍保留原有输入和输出数据，整条管线不是完全流式，总内存仍随资源规模增加；增加进程还会增加模型实例和库的常驻内存，应结合虚拟机内存峰值调整并发。

统一使用spawn，避免从Web后台线程fork继承锁。每个子进程使用threadpoolctl限制BLAS/OpenMP线程为1，避免进程数与原生线程数相乘竞争；Prophet子进程环境也设置相应线程限制。线程模式不在各线程单独修改全局原生线程设置。

异常会传播至调用方，取消排队任务并等待已开始任务退出后回收池，避免与下一轮重叠抢CPU。不会发布缺指标的部分新预测；模型本身已有失败降级照常保留并报告。

## 分辨CPU空闲的原因

日志新增实际backend、workers、available_cpus、metric_tasks、在途上限，以及每5秒左右的指标完成进度。任务结果包含执行过拟合的PID。`generation_stats.json.execution`新增：

| 字段 | 含义 |
| --- | --- |
| backend/workers/available_cpus | 实际后端、并发数、可用CPU |
| task_count/submitted/completed | 指标任务数及完成数 |
| max_in_flight/pids | 实际峰值在途任务数和执行进程ID |
| preparation_seconds | 读取、窗口准备及可选raw保存等前置耗时 |
| fit_seconds | 调度拟合阶段墙钟时间，包含池启动、传输、父进程组装和回收 |
| assembly_seconds | 上述阶段中的父进程组装时间，是子项，不重复相加 |
| model_fit_seconds_sum | 各任务拟合墙钟时间之和，并发时可大于阶段墙钟时间，不是总CPU时间 |
| postprocess_seconds | 校准、影子建议、留档、兑现评分、合并等后处理 |
| output_write_seconds | 输出分片及误差报告等写盘时间，统计文件自身写入除外 |
| elapsed_before_output_seconds | 从入口到输出写盘前的总墙钟时间 |

CPU空闲也可能来自采集等待、磁盘I/O、父进程组装或SQLite留档。查看整个进程树/虚拟机CPU，不只查看Web主进程。更多模型增加每个指标的验证、测试及重拟合成本，并行不能消除这些计算。

## 部署机基准

使用确定性合成序列调用真实现有模型，不使用空转循环。相同数据与模型对比串行、线程、多进程，核对预测、误差、选择模型及配置指纹的一致性。

```bash
python -m benchmarks.multicore_forecast \
  --jobs 32 --points 720 --workers 8 \
  --models arima,sarima,prophet,seasonal_naive,rolling_mean \
  --backends serial,thread,process \
  --output outputs/benchmarks/multicore-server.json
```

报告包含启动/IPC/回收开销、每秒指标任务数、实际PID、峰值在途数、模型失败/降级、加速比和数值一致性。后运行的后端可能受缓存预热影响，可调整顺序、扩大任务数量复测。范围只包含模型拟合，不包含采集、组装、留档与写盘；应再结合完整generation_stats评估万人规模，不能以45资源演示或小样本加速比保证生产吞吐。

本机检查样例：Windows、12逻辑CPU，8个ARIMA指标任务，每条240点；串行15.74秒，2进程10.62秒，启动开销在内约1.48×，确认2个子进程、数值一致、无模型失败。仅代表本机样例，记录在 `outputs/benchmarks/multicore-smoke.json`。
