"""
三种时间序列预测方法封装：
- ARIMA
- SARIMA (SARIMAX)
- Prophet

输入统一使用 pd.Series，要求 index 为 DatetimeIndex（等间隔更佳）。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
import warnings
from typing import Any, List, Optional, Tuple

import numpy as np
import pandas as pd
from resource_predict.settings import settings
logger = logging.getLogger(__name__)

# 全局抑制 Prophet 及其依赖的日志输出，避免每次调用冗余设置
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("prophet.plot").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class ForecastResult:
    yhat: pd.Series
    seconds: float


def usage_forecast_upper_bound(y_train: pd.Series) -> float:
    """
    预测值上界：固定 1.0 或按训练集最大值留余量（避免真实 CPU 等指标 >1 时被错误截断）。
    """
    cfg = settings.forecast
    mode = (cfg.usage_clip_upper_mode or "fixed").lower().strip()
    if mode == "fixed":
        return float(cfg.usage_clip_upper_fixed)
    if mode == "auto_train_max":
        arr = y_train.to_numpy(dtype=float)
        if arr.size == 0:
            return float(cfg.usage_clip_upper_fixed)
        m = float(np.nanmax(arr))
        slack = float(cfg.usage_clip_upper_slack)
        return max(float(cfg.usage_clip_upper_fixed), m * (1.0 + slack))
    raise ValueError(f"不支持的 usage_clip_upper_mode: {cfg.usage_clip_upper_mode!r}")


def clip_usage_range(y: pd.Series, *, upper: float, lower: float = 0.0) -> pd.Series:
    """将预测值限制在 [lower, upper]（默认 lower=0）。"""
    return y.clip(lower=lower, upper=upper)


def _is_converged(res: Any) -> bool:
    """尽量从 statsmodels 返回对象中判断是否收敛。"""
    mle_retvals = getattr(res, "mle_retvals", None)
    if isinstance(mle_retvals, dict):
        converged = mle_retvals.get("converged")
        if converged is not None:
            return bool(converged)
    return True


def ensure_regular_freq(y: pd.Series) -> pd.Series:
    """将时间索引规整为等间隔，缺值用时间插值填充。"""
    if not isinstance(y.index, pd.DatetimeIndex):
        raise TypeError("y.index 必须是 DatetimeIndex")
    y = y.sort_index()
    freq = pd.infer_freq(y.index)
    if freq is None:
        diffs = np.diff(y.index.values).astype("timedelta64[s]").astype(np.int64)
        diffs = diffs[diffs > 0]
        if diffs.size == 0:
            return y
        step_s = int(np.median(diffs))
        new_index = pd.date_range(y.index[0], y.index[-1], freq=pd.Timedelta(seconds=step_s))
        y = y.reindex(new_index)
    else:
        y = y.asfreq(freq)
    if y.isna().any():
        y = y.interpolate(method="time").ffill().bfill()
    return y


def infer_steps_per_day(dt_index: pd.DatetimeIndex) -> int:
    """根据时间间隔推断一天有多少个采样点，用于 SARIMA 的季节周期 s。"""
    if len(dt_index) < 3:
        return 24
    freq = pd.infer_freq(dt_index)
    if freq is not None:
        try:
            # 兼容 pandas 新版本：不再使用已弃用的 offset.delta
            offset = pd.tseries.frequencies.to_offset(freq)
            delta = pd.Timedelta(offset)
            minutes = delta / np.timedelta64(1, "m")
            if minutes > 0:
                return int(round(24 * 60 / float(minutes)))
        except Exception:
            pass
    diffs = np.diff(dt_index.values).astype("timedelta64[m]").astype(np.int64)
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return 24
    minutes = float(np.median(diffs))
    return max(1, int(round(24 * 60 / minutes))) if minutes > 0 else 24


def infer_pandas_freq(dt_index: pd.DatetimeIndex) -> str:
    """推断 pandas 频率字符串，供 Prophet 生成未来时间索引。"""
    freq = pd.infer_freq(dt_index)
    if freq is not None:
        return freq
    if len(dt_index) < 2:
        return "D"
    diffs = np.diff(dt_index.sort_values().values).astype("timedelta64[s]").astype(np.int64)
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return "D"
    step_s = int(np.median(diffs))
    return pd.Timedelta(seconds=step_s).resolution_string


def forecast_arima(
    y_train: pd.Series,
    steps: int,
    *,
    order: Optional[Tuple[int, int, int]] = None,
    trend: Optional[str] = None,
    auto_order: Optional[bool] = None,
) -> ForecastResult:
    """
    ARIMA 预测未来 steps 步。

    针对你当前“预测成平线”的情况：
    - 默认不再固定 (1,1,1)
    - 当 order=None 且 auto_order=True 时，会在少量候选阶数里用 AIC 自动选择
    - 显式加入线性趋势 trend='t'，并放宽 stationarity/invertibility 约束
    """
    try:
        from statsmodels.tsa.arima.model import ARIMA  # type: ignore
        from statsmodels.tools.sm_exceptions import ConvergenceWarning  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("未安装 statsmodels。请先执行: pip install statsmodels") from e

    y_train = ensure_regular_freq(y_train)
    if trend is None:
        trend = settings.forecast.arima_trend
    if auto_order is None:
        auto_order = settings.forecast.arima_auto_order

    # --- 自动选择 order（AIC 最小）---
    # 只在少量候选里搜索，保证生成速度仍可接受。
    chosen_order = order
    if (chosen_order is None) and auto_order:
        # 为了既提升形状又避免太慢：
        # - 优先候选 d=0（再结合 trend='t' 吸收趋势），降低“塌成平线”的概率
        # - 候选阶数尽量少，配合较小 maxiter，避免生成被迫中断
        candidate_orders: List[Tuple[int, int, int]] = list(settings.forecast.arima_candidate_orders)

        best_aic: float = float("inf")
        best_order: Optional[Tuple[int, int, int]] = None

        for cand in candidate_orders:
            try:
                t0_try = time.perf_counter()
                model = ARIMA(
                    y_train,
                    order=cand,
                    trend=trend,
                    enforce_stationarity=False,
                    enforce_invertibility=False,
                )
                # 拟合使用 statsmodels 默认估计器
                # 这里主要通过 maxiter 限制每次搜索的耗时。
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", ConvergenceWarning)
                    res_try = model.fit(method_kwargs={"maxiter": settings.forecast.arima_maxiter})
                has_conv_warn = any(issubclass(w.category, ConvergenceWarning) for w in caught)
                if has_conv_warn or (not _is_converged(res_try)):
                    # 收敛告警时自动重试一次，保留首次结果作为兜底
                    res_fallback = res_try
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", ConvergenceWarning)
                        res_try = model.fit(
                            method_kwargs={"maxiter": settings.forecast.arima_maxiter * 2}
                        )
                    # 若重试后 AIC 更差（或同样未收敛），回退到首次结果
                    aic_retry = float(getattr(res_try, "aic", float("inf")))
                    aic_fallback = float(getattr(res_fallback, "aic", float("inf")))
                    if aic_retry > aic_fallback:
                        res_try = res_fallback
                aic = float(getattr(res_try, "aic", float("inf")))
                _ = time.perf_counter() - t0_try
                if aic < best_aic:
                    best_aic = aic
                    best_order = cand
            except Exception:
                continue

        # 若都失败，回退到原先的 (1,1,1)
        chosen_order = best_order if best_order is not None else settings.forecast.arima_default_order

    if chosen_order is None:
        chosen_order = settings.forecast.arima_default_order

    t0 = time.perf_counter()
    model = ARIMA(
        y_train,
        order=chosen_order,
        trend=trend,
        enforce_stationarity=False,
        enforce_invertibility=False,
    )
    # 最终用选好的 order + trend 进行拟合并预测
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        res = model.fit(method_kwargs={"maxiter": settings.forecast.arima_maxiter})
    has_conv_warn = any(issubclass(w.category, ConvergenceWarning) for w in caught)
    if has_conv_warn or (not _is_converged(res)):
        # 最终拟合出现收敛告警时，放宽迭代次数重试一次，并静默告警输出
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            res = model.fit(method_kwargs={"maxiter": settings.forecast.arima_maxiter * 2})
    cap = usage_forecast_upper_bound(y_train)
    yhat = clip_usage_range(res.forecast(steps=steps), upper=cap)
    seconds = time.perf_counter() - t0
    return ForecastResult(yhat=yhat, seconds=seconds)


def forecast_sarima(
    y_train: pd.Series,
    steps: int,
    *,
    order: Optional[Tuple[int, int, int]] = None,
    seasonal_order: Optional[Tuple[int, int, int, int]] = None,
) -> ForecastResult:
    """SARIMA 预测未来 steps 步；如 seasonal_order 未给则按“一天采样点数”推断 s。"""
    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX  # type: ignore
        from statsmodels.tools.sm_exceptions import ConvergenceWarning  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("未安装 statsmodels。请先执行: pip install statsmodels") from e

    y_train = ensure_regular_freq(y_train)
    cfg = settings.forecast
    if order is None:
        order = cfg.sarima_order
    if seasonal_order is None:
        s = infer_steps_per_day(y_train.index)
        s = max(1, min(int(s), int(cfg.sarima_max_seasonal_period)))
        p_s, d_s, q_s = cfg.sarima_seasonal_pdq
        seasonal_order = (p_s, d_s, q_s, s)
    t0 = time.perf_counter()
    model = SARIMAX(
        y_train,
        order=order,
        seasonal_order=seasonal_order,
        simple_differencing=cfg.sarima_simple_differencing,
        enforce_stationarity=False,
        enforce_invertibility=False,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        res = model.fit(disp=False, method=cfg.sarima_optimizer, maxiter=cfg.sarima_maxiter)
    has_conv_warn = any(issubclass(w.category, ConvergenceWarning) for w in caught)
    if cfg.sarima_retry_on_convergence and (has_conv_warn or (not _is_converged(res))):
        # SARIMA 对优化器较敏感：收敛告警时切到 powell 并增加迭代次数重试
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            res = model.fit(
                disp=False,
                method="powell",
                maxiter=cfg.sarima_maxiter * 2,
            )
    cap = usage_forecast_upper_bound(y_train)
    yhat = clip_usage_range(res.get_forecast(steps=steps).predicted_mean, upper=cap)
    seconds = time.perf_counter() - t0
    return ForecastResult(yhat=yhat, seconds=seconds)


def forecast_prophet(
    y_train: pd.Series,
    steps: int,
    *,
    freq: Optional[str] = None,
    seasonality_mode: Optional[str] = None,
    daily_seasonality: Optional[bool] = None,
    weekly_seasonality: Optional[bool] = None,
    yearly_seasonality: Optional[bool] = None,
    changepoint_prior_scale: Optional[float] = None,
    seasonality_prior_scale: Optional[float] = None,
) -> ForecastResult:
    """Prophet 预测未来 steps 步。"""
    y_train = ensure_regular_freq(y_train)
    try:
        from prophet import Prophet  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("未安装 prophet。请先执行: pip install prophet") from e

    if freq is None:
        freq = infer_pandas_freq(y_train.index)
    if seasonality_mode is None:
        seasonality_mode = settings.forecast.prophet_seasonality_mode
    if daily_seasonality is None:
        daily_seasonality = settings.forecast.prophet_daily_seasonality
    if weekly_seasonality is None:
        weekly_seasonality = settings.forecast.prophet_weekly_seasonality
    if yearly_seasonality is None:
        yearly_seasonality = settings.forecast.prophet_yearly_seasonality
    if changepoint_prior_scale is None:
        changepoint_prior_scale = float(settings.forecast.prophet_changepoint_prior_scale)
    if seasonality_prior_scale is None:
        seasonality_prior_scale = float(settings.forecast.prophet_seasonality_prior_scale)

    t0 = time.perf_counter()
    train_df = pd.DataFrame({"ds": y_train.index, "y": y_train.values})
    model = Prophet(
        stan_backend="CMDSTANPY",
        seasonality_mode=seasonality_mode,
        daily_seasonality=daily_seasonality,
        weekly_seasonality=weekly_seasonality,
        yearly_seasonality=yearly_seasonality,
        changepoint_prior_scale=changepoint_prior_scale,
        seasonality_prior_scale=seasonality_prior_scale,
    )
    model.fit(train_df)
    future = model.make_future_dataframe(periods=steps, freq=freq, include_history=False)
    fcst = model.predict(future)
    yhat = pd.Series(fcst["yhat"].values, index=future["ds"], name="yhat")
    cap = usage_forecast_upper_bound(y_train)
    yhat = clip_usage_range(yhat, upper=cap)
    seconds = time.perf_counter() - t0
    return ForecastResult(yhat=yhat, seconds=seconds)

