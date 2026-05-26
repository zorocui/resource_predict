from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class AppConfig:
    # Flask 静态资源目录，通常对应项目根目录下的 static/。
    static_folder: str = "static"
    # Flask 模板目录，通常对应项目根目录下的 templates/。
    template_folder: str = "templates"
    # 预测产物、原始数据、日志和任务记录的默认输出目录。
    out_dir: str = "outputs"
    # 日志文件名；为 None 时不写文件日志。
    log_file: Optional[str] = "resource_predict.log"
    # 日志级别，例如 DEBUG、INFO、WARNING、ERROR。
    log_level: str = "INFO"
    # 是否同时把日志输出到控制台，便于本地调试和查看启动信息。
    log_console: bool = True
    # Flask 监听地址；0.0.0.0 表示允许局域网其他机器访问。
    host: str = "0.0.0.0"
    # Flask 监听端口。
    port: int = 5000
    # Flask debug 模式；生产或联调环境建议保持 False。
    debug: bool = False


@dataclass(frozen=True)
class GenerationConfig:
    # 演示数据默认生成的 VM 资源数量。
    resources: int = 15
    # 每个资源每个指标的历史时间点数量。
    n: int = 240
    # 从历史序列尾部切出的测试集点数，用于评估模型 RMSE。
    test_size: int = 72
    # 向未来预测的点数；当前 freq="h" 时 24 表示预测未来 24 小时。
    future_steps: int = 24
    # 演示数据随机种子基准值，保证多次生成结果可复现。
    base_seed: int = 1000
    # 时间序列频率，传给 pandas/预测流程；"h" 表示小时级数据。
    freq: str = "h"
    # 预测并行 worker 数；None 表示由程序按机器资源自动决定。
    max_workers: Optional[int] = None
    # details/ 详情分片大小；资源很多时避免单个详情 JSON 过大。
    detail_chunk_size: int = 200
    # 是否保存 scoped raw_data.json；增量更新和详情合并依赖该文件。
    save_raw_dataset: bool = True
    # 列表接口未显式指定 top_n 时的默认返回 TopN 数量。
    top_n_default: int = 20
    # 列表接口未显式指定 page_size 时的默认分页大小。
    api_page_size_default: int = 20
    # 列表接口允许的最大 page_size，防止一次请求返回过多数据。
    api_page_size_max: int = 200


@dataclass(frozen=True)
class ForecastConfig:
    # 启用的预测模型集合；可包含 arima、sarima、prophet。
    enabled_methods: Tuple[str, ...] = ("seasonal_naive", "rolling_mean", "prophet")
    # 预测使用率上限裁剪模式：auto_train_max 会参考训练集最大值自动放宽。
    usage_clip_upper_mode: str = "auto_train_max"
    # fixed 裁剪模式下的固定上限；1.0 表示 100%。
    usage_clip_upper_fixed: float = 1.0
    # auto_train_max 模式下，在训练集最大值基础上额外放宽的余量。
    usage_clip_upper_slack: float = 0.03
    # Prophet 季节性模式；additive 适合大多数使用率序列。
    prophet_seasonality_mode: str = "additive"
    # Prophet 是否启用日季节性，用于捕捉一天内的周期波动。
    prophet_daily_seasonality: bool = True
    # Prophet 是否启用周季节性，用于捕捉工作日/周末差异。
    prophet_weekly_seasonality: bool = True
    # Prophet 是否启用年季节性；当前小时级短历史数据默认关闭。
    prophet_yearly_seasonality: bool = False
    # Prophet 趋势变化灵活度；越大越容易跟随突变，也更容易过拟合。
    prophet_changepoint_prior_scale: float = 0.05
    # Prophet 季节性强度先验；越大季节性曲线越灵活。
    prophet_seasonality_prior_scale: float = 10.0
    # Rolling backtest folds used for model selection stability. 1 keeps the old single holdout behavior.
    rolling_backtest_folds: int = 3
    # Whether to add an inverse-error weighted ensemble candidate to model selection.
    enable_ensemble: bool = True
    # Recent robust z-score threshold that routes anomalous series to robust candidates.
    anomaly_route_zscore_threshold: float = 3.5


@dataclass(frozen=True)
class DecisionConfig:
    # VM 指标 P95 达到该使用率时，触发扩容判断。
    scale_out_threshold: float = 0.8
    # VM 指标平均值低于该使用率时，进入缩容候选判断。
    scale_in_threshold: float = 0.2
    # 缩容保护阈值：即使平均值低，P95 高于该值也不建议缩容。
    scale_in_p95_guard: float = 0.35
    # 连续高负载判断阈值，用于识别持续压力。
    consecutive_high_threshold: float = 0.8
    # 连续低负载判断阈值，用于识别持续空闲。
    consecutive_low_threshold: float = 0.2
    # 连续高/低负载至少需要持续的点数。
    consecutive_points: int = 3
    # 峰谷差阈值；波动过大时会影响缩容稳定性判断。
    peak_valley_gap_threshold: float = 0.3
    # 峰值保护阈值；预测峰值过高时倾向扩容或避免缩容。
    peak_guard_threshold: float = 0.85
    # 趋势判断窗口点数，用最近多少个预测点计算上升/下降趋势。
    trend_window_points: int = 6
    # 上升趋势斜率阈值；超过该值表示负载有明显上升趋势。
    uptrend_slope_threshold: float = 0.012
    # 下降趋势斜率阈值；低于该值表示负载有明显下降趋势。
    downtrend_slope_threshold: float = -0.012
    # 窗口均值变化阈值，用于判断最近窗口相对前序窗口是否明显变化。
    window_mean_delta_threshold: float = 0.08
    # 扩容目标利用率；按预测负载反推目标规格时希望扩容后压到该利用率附近。
    scale_out_target_utilization: float = 0.8
    # 扩容容量负载阈值；目标规格计算时用于判断当前容量是否已被打满。
    scale_out_capacity_load_threshold: float = 1.0
    # 是否把建议 CPU 核数对齐到偶数，贴近常见云主机规格。
    snap_target_cpu_cores_to_even: bool = True
    # 单次缩容最大降幅比例；0.5 表示最多缩到当前规格的一半。
    scale_in_max_reduction_ratio: float = 0.5
    # Policy tier controls how early and how confidently recommendations fire.
    default_policy_tier: str = "balanced"
    conservative_namespaces: Tuple[str, ...] = ("prod", "production", "payments", "core", "platform")
    aggressive_namespaces: Tuple[str, ...] = ("dev", "test", "staging", "batch")
    # How many consistent rounds should be observed before executing a non-hold action.
    scale_out_confirmations: int = 2
    scale_in_confirmations: int = 3
    # Minimum cooldown guidance for operators or future executors.
    scale_out_cooldown_minutes: int = 60
    scale_in_cooldown_minutes: int = 360


@dataclass(frozen=True)
class UpdateConfig:
    # 是否启用后台定时 Pull 增量更新；关闭时仍可手动调用更新 API。
    enabled: bool = False
    # 定时 Pull 更新间隔，单位分钟。
    interval_minutes: int = 60
    # 每次调用增量 provider 期望追加的时间点数量。
    points_per_update: int = 1
    # 是否在增量更新后保持滑动窗口长度，避免历史序列无限增长。
    sliding_window: bool = False
    # 前端详情展示窗口点数；0 表示展示全部，不裁剪训练数据。
    display_window_points: int = 0
    # 自定义增量 provider 路径，格式为 "module:function"；为空时使用默认 mock provider。
    incremental_provider_path: str = ""


@dataclass(frozen=True)
class K8SPrometheusTarget:
    # 集群标识，会写入 resource_id 和资源 spec，避免多集群同名 Pod 冲突。
    cluster: str = ""
    # Prometheus HTTP API 基础地址，例如 http://127.0.0.1:9090。
    prometheus_url: str = ""
    # 该集群的命名空间过滤正则；为空时继承全局 namespace_regex。
    namespace_regex: str = ""
    # Bearer Token 鉴权内容；为空表示不使用 Bearer 鉴权。
    bearer_token: str = ""
    # Basic Auth 鉴权内容，值为 base64(user:password)；为空表示不使用 Basic Auth。
    basic_auth: str = ""


@dataclass(frozen=True)
class K8SPrometheusConfig:
    # Prometheus 目标集群列表；为空时可通过 K8S_PROMETHEUS_CLUSTERS 环境变量提供。
    clusters: Tuple[K8SPrometheusTarget, ...] = ()
    # 从 Prometheus 拉取最近多少天的历史数据。
    history_days: int = 7
    # Prometheus range query 步长，单位秒；300 表示 5 分钟一个点。
    step_seconds: int = 300
    # 全局命名空间过滤正则；单集群未配置 namespace_regex 时使用该值。
    namespace_regex: str = ""
    # Prometheus HTTP 请求超时时间，单位秒。
    request_timeout_seconds: int = 30
    # 多集群拉取时是否遇到任一集群失败就立即中断；False 表示尽量保留成功集群。
    fail_fast: bool = False


@dataclass(frozen=True)
class Settings:
    # Web、日志和输出目录配置。
    app: AppConfig = field(default_factory=AppConfig)
    # 数据生成、预测窗口、分页和输出分片配置。
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    # 预测模型、Prophet 参数和预测值裁剪配置。
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    # VM 扩缩容建议的业务阈值和目标规格计算配置。
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    # 增量更新、定时更新和展示窗口配置。
    update: UpdateConfig = field(default_factory=UpdateConfig)
    # K8S Workload Prometheus 数据接入配置。
    k8s_prometheus: K8SPrometheusConfig = field(default_factory=K8SPrometheusConfig)


# 全局默认配置实例；业务代码统一从这里读取默认设置。
settings = Settings()
