from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class AppConfig:
    # Flask 静态资源目录（前端 js/css 与输出图数据目录的上级目录）
    static_folder: str = "static"
    # Flask 模板目录（HTML 模板位置）
    template_folder: str = "templates"
    # 预测结果输出目录（manifest.json 的所在目录）
    out_dir: str = "outputs"
    # 前端页面读取的清单文件名
    manifest_filename: str = "manifest.json"
    # 大规模模式下的摘要索引文件名（列表接口读取）
    summary_index_filename: str = "summary_index.json"
    # 详情分片目录名（单资源详情接口读取）
    details_dirname: str = "details"
    # 原始观测数据文件名（与预测输出同目录，仅含 metrics，不含模型预测）
    raw_data_filename: str = "raw_data.json"
    # 应用日志文件名（置于 out_dir 下）；设为 None 或空字符串则不写文件
    log_file: Optional[str] = "resource_predict.log"
    # 日志级别：DEBUG / INFO / WARNING / ERROR
    log_level: str = "INFO"
    # 是否同时向 stderr 输出（与 log_file 独立）
    log_console: bool = True
    # Web 服务监听地址（本机调试常用 127.0.0.1，服务器可改 0.0.0.0）
    host: str = "0.0.0.0"
    # Web 服务端口
    port: int = 5000
    # Flask 调试开关（生产环境建议 False）
    debug: bool = False


@dataclass(frozen=True)
class GenerationConfig:
    # 预测结果输出目录（通常与 AppConfig.out_dir 保持一致）
    out_dir: str = "outputs"
    # 资源数量（每个资源会生成 cpu/memory/disk 三组预测）
    resources: int = 15
    # 每条指标序列总长度（历史点数量）
    n: int = 240
    # 测试集长度（用最后 test_size 个点做评估与对比）
    test_size: int = 72
    # 未来预测步长（默认与 test_size 一致）
    future_steps: int = 24
    # 随机种子起点（演示数据场景下用于复现实验）
    base_seed: int = 1000
    # 并发 worker 数；None 表示按代码中的默认策略自动计算
    max_workers: Optional[int] = None
    # 单资源内 cpu / memory / disk 是否并行拟合（每资源再开小型线程池，提高多核占用）
    parallel_metrics: bool = True
    # 指标级并行池大小（对应三条序列，一般保持 3）
    parallel_metrics_max_workers: int = 3
    # 为 True 且 max_workers 未在参数与配置中显式指定时：外层 worker 按约「每 3 条指标占满一套核」收缩，
    # 减轻 Prophet/Stan 与 BLAS 多线程叠加的过订阅；追求极限吞吐可设为 False 并配合环境变量 OMP_NUM_THREADS=1
    parallel_metrics_balance_outer_workers: bool = False
    # 时间序列频率（如 "h"=小时，"15min"=15分钟）
    freq: str = "h"
    # 模型耗时统计输出模式：
    # - "on"  : 始终输出
    # - "off" : 始终关闭
    # - "auto": 按资源数量自动决定（见 timing_stats_auto_resources_threshold）
    timing_stats_mode: str = "auto"
    # 当 timing_stats_mode="auto" 时，resources 小于等于该阈值则开启耗时统计
    timing_stats_auto_resources_threshold: int = 20
    # 详情数据分片大小（每个 details 文件包含的资源数）
    detail_chunk_size: int = 200
    # 全量生成时是否写入 raw_data.json（原始序列与预测分离；仅重跑预测时不要改此项为 False 以外逻辑）
    save_raw_dataset: bool = True
    # 首页默认展示 TopN 资源（按紧迫度分数排序）
    top_n_default: int = 20
    # 列表接口默认分页大小
    api_page_size_default: int = 20
    # 列表接口允许的最大分页大小
    api_page_size_max: int = 200
    # 演示数据：raw 经线性映射后再 /100，得到使用率小数 y∈[0,1]
    # cpu：y = clip((raw * cpu_scale + cpu_offset) / 100, 0, 1)
    cpu_scale: float = 3.0
    # cpu 映射的偏移量
    cpu_offset: float = 20.0
    # memory 映射的缩放系数
    memory_scale: float = 2.4
    # memory 映射的偏移量
    memory_offset: float = 15.0
    # disk 映射的缩放系数
    disk_scale: float = 2.2
    # disk 映射的偏移量
    disk_offset: float = 10.0


@dataclass(frozen=True)
class ForecastConfig:
    # 是否启用 ARIMA 预测
    enable_arima: bool = False
    # 是否启用 SARIMA 预测
    enable_sarima: bool = False
    # 是否启用 Prophet 预测
    enable_prophet: bool = True

    # ARIMA
    # 趋势项设置（"n"/"c"/"t"/"ct"）
    arima_trend: str = "t"
    # 是否启用自动阶数搜索（基于候选集 AIC 选择）
    arima_auto_order: bool = True
    # 自动搜索关闭或失败时的兜底阶数
    arima_default_order: Tuple[int, int, int] = (1, 1, 1)
    # 自动搜索候选阶数组合（数量越多越慢）
    arima_candidate_orders: Tuple[Tuple[int, int, int], ...] = ((1, 0, 1), (2, 0, 2))
    # ARIMA 拟合最大迭代次数
    arima_maxiter: int = 25

    # SARIMA
    # SARIMA 非季节部分阶数 (p, d, q)
    sarima_order: Tuple[int, int, int] = (1, 1, 1)
    # SARIMA 季节部分阶数 (P, D, Q)，周期 s 会自动推断
    sarima_seasonal_pdq: Tuple[int, int, int] = (1, 1, 1)
    # SARIMA 推断出的季节周期 s 上限（过大会显著增加耗时）
    sarima_max_seasonal_period: int = 24
    # 启用简单差分以加速 SARIMA 拟合
    sarima_simple_differencing: bool = False
    # SARIMA 主优化器（常用 lbfgs / powell）
    sarima_optimizer: str = "lbfgs"
    # SARIMA 拟合最大迭代次数
    sarima_maxiter: int = 35
    # 收敛失败时是否启用慢速兜底重试（开启更稳，关闭更快）
    sarima_retry_on_convergence: bool = False

    # 预测值裁剪上界（真实数据可能超过 1，例如 CPU 负载口径非“核占比”时）
    # - "fixed"：上限恒为 usage_clip_upper_fixed（默认 1.0，兼容纯 [0,1] 使用率）
    # - "auto_train_max"：上限 = max(usage_clip_upper_fixed, train_max * (1 + usage_clip_upper_slack))
    usage_clip_upper_mode: str = "auto_train_max"
    usage_clip_upper_fixed: float = 1.0
    usage_clip_upper_slack: float = 0.03

    # Prophet
    # 季节性叠加方式（"additive" 或 "multiplicative"）
    prophet_seasonality_mode: str = "additive"
    # 是否启用日季节性
    prophet_daily_seasonality: bool = True
    # 是否启用周季节性（小时级监控通常有周内模式）
    prophet_weekly_seasonality: bool = True
    # 是否启用年季节性
    prophet_yearly_seasonality: bool = False
    # 趋势变点先验（越小趋势越平滑，越大越跟历史拐点）
    prophet_changepoint_prior_scale: float = 0.05
    # 季节性分量先验（越大越允许拟合更强周期幅度）
    prophet_seasonality_prior_scale: float = 10.0


@dataclass(frozen=True)
class UpdateConfig:
    # 是否启用定时数据更新（后台线程）。
    # 当前为 False：仅支持手动触发（POST /api/update-trigger），后续改为 True 即可开启自动更新。
    enabled: bool = False
    # 数据拉取间隔（分钟）
    interval_minutes: int = 60
    # 每次更新新增的数据点数
    points_per_update: int = 1
    # 若为 True：每次合并增量后，按「净新增」点数从序列头部丢弃等量最旧点，
    # 使 raw_data.json 内该指标长度与更新前一致（会永久丢失被裁掉的早期数据）。
    # 若为 False：raw 文件保留全量历史（推荐用于预测训练）。
    sliding_window: bool = False
    # 前端 ECharts 展示窗口大小（数据点数）。0 表示展示全部历史数据；
    # 大于 0 时仅截取最近 N 个观测点用于图表（不写回 raw，不影响模型训练数据）。
    display_window_points: int = 0
    # ---------- 以下为内网真实数据接入预留 ----------
    # 自定义增量数据源（Python 导入路径，格式如 "my_module:my_provider"）。
    # 函数签名需符合 IncrementalProvider：
    #   (prepared_resources: List[Dict], points_to_add: int) -> List[Dict]
    # 留空则回退到 resource_predict.providers.mock.mock_incremental_provider。
    # 内网部署时：改为你的 provider 路径，或通过 POST /api/update-data 直接推送数据。
    incremental_provider_path: str = ""


@dataclass(frozen=True)
class K8SPrometheusConfig:
    # Prometheus HTTP API 地址；留空时 provider 会从环境变量 K8S_PROMETHEUS_URL 读取。
    prometheus_url: str = ""
    # 集群标识，写入 resource_id 与 spec.cluster。
    cluster: str = "cluster-k8s-a"
    # 默认拉取最近 7 天。
    history_days: int = 7
    # query_range 与重采样步长，默认 5 分钟。
    step_seconds: int = 300
    # 可选 namespace 正则，用于缩小首批接入范围。
    namespace_regex: str = ""
    # request/limit 静态指标查询时间点默认取 now。
    request_timeout_seconds: int = 30


@dataclass(frozen=True)
class DecisionConfig:
    # 多数阈值为 [0,1] 小数（0.8 表示 80%）；预测序列在部分口径下可超过 1（>100%），见 scale_out_capacity_load_threshold。
    # 扩容阈值：未来窗口 P95 超过该值触发高负载信号
    scale_out_threshold: float = 0.8
    # 缩容阈值：未来窗口均值低于该值，且满足保护条件时触发
    scale_in_threshold: float = 0.2
    # 缩容保护：未来窗口 P95 需低于该值，避免“低均值但有高峰”误缩容
    scale_in_p95_guard: float = 0.35
    # 连续高负载判定阈值
    consecutive_high_threshold: float = 0.8
    # 连续低负载判定阈值
    consecutive_low_threshold: float = 0.2
    # 连续点阈值：达到该连续长度才认为是持续负载
    consecutive_points: int = 3
    # 峰谷差阈值（max-min）：用于识别大幅波动且峰值偏高的场景
    peak_valley_gap_threshold: float = 0.3
    # 高峰保护阈值：峰值超过该值时更偏向扩容而非缩容
    peak_guard_threshold: float = 0.85
    # 趋势窗口长度：用于计算短期斜率与前后窗口均值差
    trend_window_points: int = 6
    # 斜率阈值（每点使用率变化，0~1 刻度）：超过该值认为明显上升
    uptrend_slope_threshold: float = 0.012
    # 斜率阈值（每点使用率变化）：低于该值认为明显下降
    downtrend_slope_threshold: float = -0.012
    # 前后窗口均值差阈值：用于趋势确认
    window_mean_delta_threshold: float = 0.08
    # 扩容目标利用率（小数）：预测 P95/峰值超过 100% 时，按「等效负载 / 目标利用率」推算所需容量，
    # 使扩容后预测使用率大致回落到该水平以下（略留余量，默认 85%）。
    scale_out_target_utilization: float = 0.8
    # 当 max(P95, 峰值) 超过该值（>1 即超过 100%）时启用上述比例推算，并与分档放大系数取较大者。
    scale_out_capacity_load_threshold: float = 1.0
    # 建议目标规格中 CPU/内存/硬盘 对齐为偶数（云厂商常见规格）；奇数则向上取到下一个偶数。hold 不改变当前规格故不调整。
    # 硬盘缩容时最小规格为50GB。
    snap_target_cpu_cores_to_even: bool = True
    # 单次缩容最大缩减比例（0~1 小数）：如 0.5 表示单步最多缩减 50%，防止过激缩容
    scale_in_max_reduction_ratio: float = 0.5
    # -------- 列表“紧迫度分数”权重配置（用于 API 排序与展示）--------
    # 置信度加分
    urgency_confidence_high: float = 30.0
    urgency_confidence_medium: float = 15.0
    urgency_confidence_low: float = 5.0
    urgency_confidence_default: float = 10.0
    # 扩容压力分：p95 超过该阈值开始计分（基础段，0~1）
    urgency_out_p95_base_threshold: float = 0.75
    urgency_out_p95_base_weight: float = 1.4
    # 扩容压力分：p95 高压段附加计分（强化高负载）
    urgency_out_p95_high_threshold: float = 0.9
    urgency_out_p95_high_weight: float = 2.2
    # 扩容压力分：峰值超阈值计分
    urgency_out_peak_threshold: float = 0.92
    urgency_out_peak_weight: float = 1.2
    # 扩容压力分：极高峰值附加计分
    urgency_out_peak_extreme_threshold: float = 0.97
    urgency_out_peak_extreme_weight: float = 1.8
    # 缩容压力分：avg/p95 越低，缩容越紧迫（保守权重，避免压过扩容）
    urgency_in_avg_threshold: float = 0.35
    urgency_in_avg_weight: float = 0.55
    urgency_in_p95_threshold: float = 0.3
    urgency_in_p95_weight: float = 0.75


@dataclass(frozen=True)
class Settings:
    # Web 与页面展示相关配置集合
    app: AppConfig = field(default_factory=AppConfig)
    # 数据生成与任务调度相关配置集合
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    # 各预测模型默认超参数配置集合
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    # 扩缩容建议规则配置集合
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    # 定时数据更新相关配置集合
    update: UpdateConfig = field(default_factory=UpdateConfig)
    # K8S Pod Prometheus 接入配置。
    k8s_prometheus: K8SPrometheusConfig = field(default_factory=K8SPrometheusConfig)


# 全局配置入口：后续只改这里即可统一生效
settings = Settings()

