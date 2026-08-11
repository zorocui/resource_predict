from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd


RUNTIME_CONFIG_PATH = Path("deploy") / "runtime_config.json"
LEGACY_FORECAST_CONFIG_PATH = Path("deploy") / "forecast_config.json"
SUPPORTED_FORECAST_METHODS = ("arima", "sarima", "prophet", "seasonal_naive", "rolling_mean")
SUPPORTED_POLICY_TIERS = ("conservative", "balanced", "aggressive")


class RuntimeConfigValidationError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class CollectionConfig:
    scheduled_update_enabled: bool = True
    scheduled_update_interval_minutes: int = 360
    history_days: int = 7
    step_seconds: int = 600
    rate_window: str = "15m"
    request_timeout_seconds: int = 300
    range_query_chunk_hours: int = 24
    request_max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    max_interpolation_gap_steps: int = 3


@dataclass(frozen=True)
class PredictionConfig:
    vm_test_duration: str = "72h"
    vm_future_duration: str = "24h"
    workload_test_duration: str = "24h"
    workload_future_duration: str = "24h"
    enabled_methods: tuple[str, ...] = ("seasonal_naive", "prophet")
    enable_ensemble: bool = False


@dataclass(frozen=True)
class DecisionConfig:
    default_policy_tier: str = "balanced"
    scale_out_threshold: float = 0.8
    scale_in_threshold: float = 0.2
    scale_in_max_reduction_ratio: float = 0.5
    scale_out_confirmations: int = 2
    scale_in_confirmations: int = 3
    scale_out_cooldown_minutes: int = 60
    scale_in_cooldown_minutes: int = 360
    conservative_namespaces: tuple[str, ...] = ("prod", "production", "payments", "core", "platform")
    aggressive_namespaces: tuple[str, ...] = ("dev", "test", "staging", "batch")


@dataclass(frozen=True)
class RuntimeConfig:
    collection: CollectionConfig = CollectionConfig()
    prediction: PredictionConfig = PredictionConfig()
    decision: DecisionConfig = DecisionConfig()


def default_runtime_config() -> RuntimeConfig:
    return RuntimeConfig()


def runtime_config_to_dict(config: RuntimeConfig) -> dict[str, Any]:
    return asdict(config)


def _error(path: str, message: str) -> RuntimeConfigValidationError:
    return RuntimeConfigValidationError(path, message)


def _section(payload: dict[str, Any], name: str, defaults: Any) -> dict[str, Any]:
    value = payload.get(name, {})
    if not isinstance(value, dict):
        raise _error(f"runtime.{name}", f"{name} 必须是对象")
    allowed = set(asdict(defaults))
    unknown = set(value) - allowed
    if unknown:
        key = sorted(unknown)[0]
        raise _error(f"runtime.{name}.{key}", f"未知配置字段: {key}")
    return {**asdict(defaults), **value}


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error(path, "必须为布尔值")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _error(path, f"{path.rsplit('.', 1)[-1]} 必须为正整数")
    return value


def _positive_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(path, f"{path.rsplit('.', 1)[-1]} 必须为有限正数")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise _error(path, f"{path.rsplit('.', 1)[-1]} 必须为有限正数")
    return result


def _ratio(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(path, "必须是 0 到 1 之间的数值")
    result = float(value)
    if not 0 <= result <= 1:
        raise _error(path, "必须是 0 到 1 之间的数值")
    return result


def _duration(value: Any, path: str) -> str:
    text = str(value or "").strip()
    try:
        delta = pd.Timedelta(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error(path, "必须是有效的正时长，例如 24h") from exc
    if delta.total_seconds() <= 0:
        raise _error(path, "必须是有效的正时长，例如 24h")
    return text


def _namespaces(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _error(path, "必须是命名空间数组")
    result: list[str] = []
    for raw in value:
        name = str(raw or "").strip()
        if not name:
            raise _error(path, "命名空间不能为空")
        if name not in result:
            result.append(name)
    return tuple(result)


def normalize_runtime_config(payload: Any) -> RuntimeConfig:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _error("runtime", "runtime 必须是对象")
    unknown = set(payload) - {"collection", "prediction", "decision"}
    if unknown:
        key = sorted(unknown)[0]
        raise _error(f"runtime.{key}", f"未知配置分区: {key}")
    defaults = default_runtime_config()
    c = _section(payload, "collection", defaults.collection)
    p = _section(payload, "prediction", defaults.prediction)
    d = _section(payload, "decision", defaults.decision)

    rate_window = str(c["rate_window"] or "").strip()
    if not re.fullmatch(r"[1-9]\d*[smhd]", rate_window):
        raise _error("runtime.collection.rate_window", "必须是正整数加 s/m/h/d，例如 15m")
    collection = CollectionConfig(
        scheduled_update_enabled=_bool(c["scheduled_update_enabled"], "runtime.collection.scheduled_update_enabled"),
        scheduled_update_interval_minutes=_positive_int(c["scheduled_update_interval_minutes"], "runtime.collection.scheduled_update_interval_minutes"),
        history_days=_positive_int(c["history_days"], "runtime.collection.history_days"),
        step_seconds=_positive_int(c["step_seconds"], "runtime.collection.step_seconds"),
        rate_window=rate_window,
        request_timeout_seconds=_positive_int(c["request_timeout_seconds"], "runtime.collection.request_timeout_seconds"),
        range_query_chunk_hours=_positive_int(c["range_query_chunk_hours"], "runtime.collection.range_query_chunk_hours"),
        request_max_attempts=_positive_int(c["request_max_attempts"], "runtime.collection.request_max_attempts"),
        retry_backoff_seconds=_positive_float(c["retry_backoff_seconds"], "runtime.collection.retry_backoff_seconds"),
        max_interpolation_gap_steps=_positive_int(c["max_interpolation_gap_steps"], "runtime.collection.max_interpolation_gap_steps"),
    )

    methods_raw = p["enabled_methods"]
    if not isinstance(methods_raw, (list, tuple)) or not methods_raw:
        raise _error("runtime.prediction.enabled_methods", "至少启用一个预测模型")
    methods = tuple(dict.fromkeys(str(x).strip() for x in methods_raw))
    invalid = [x for x in methods if x not in SUPPORTED_FORECAST_METHODS]
    if invalid:
        raise _error("runtime.prediction.enabled_methods", f"不支持的预测模型: {invalid[0]}")
    prediction = PredictionConfig(
        vm_test_duration=_duration(p["vm_test_duration"], "runtime.prediction.vm_test_duration"),
        vm_future_duration=_duration(p["vm_future_duration"], "runtime.prediction.vm_future_duration"),
        workload_test_duration=_duration(p["workload_test_duration"], "runtime.prediction.workload_test_duration"),
        workload_future_duration=_duration(p["workload_future_duration"], "runtime.prediction.workload_future_duration"),
        enabled_methods=methods,
        enable_ensemble=_bool(p["enable_ensemble"], "runtime.prediction.enable_ensemble"),
    )

    tier = str(d["default_policy_tier"] or "").strip().lower()
    if tier not in SUPPORTED_POLICY_TIERS:
        raise _error("runtime.decision.default_policy_tier", "策略等级必须是 conservative、balanced 或 aggressive")
    out_threshold = _ratio(d["scale_out_threshold"], "runtime.decision.scale_out_threshold")
    in_threshold = _ratio(d["scale_in_threshold"], "runtime.decision.scale_in_threshold")
    if in_threshold >= out_threshold:
        raise _error("runtime.decision.scale_in_threshold", "缩容阈值必须小于扩容阈值")
    decision = DecisionConfig(
        default_policy_tier=tier,
        scale_out_threshold=out_threshold,
        scale_in_threshold=in_threshold,
        scale_in_max_reduction_ratio=_ratio(d["scale_in_max_reduction_ratio"], "runtime.decision.scale_in_max_reduction_ratio"),
        scale_out_confirmations=_positive_int(d["scale_out_confirmations"], "runtime.decision.scale_out_confirmations"),
        scale_in_confirmations=_positive_int(d["scale_in_confirmations"], "runtime.decision.scale_in_confirmations"),
        scale_out_cooldown_minutes=_positive_int(d["scale_out_cooldown_minutes"], "runtime.decision.scale_out_cooldown_minutes"),
        scale_in_cooldown_minutes=_positive_int(d["scale_in_cooldown_minutes"], "runtime.decision.scale_in_cooldown_minutes"),
        conservative_namespaces=_namespaces(d["conservative_namespaces"], "runtime.decision.conservative_namespaces"),
        aggressive_namespaces=_namespaces(d["aggressive_namespaces"], "runtime.decision.aggressive_namespaces"),
    )
    return RuntimeConfig(collection=collection, prediction=prediction, decision=decision)


def write_runtime_config(config: RuntimeConfig, path: Path | str = RUNTIME_CONFIG_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(runtime_config_to_dict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_runtime_config(
    path: Path | str = RUNTIME_CONFIG_PATH,
    legacy_forecast_path: Path | str = LEGACY_FORECAST_CONFIG_PATH,
) -> tuple[RuntimeConfig, list[str]]:
    target = Path(path)
    if target.exists():
        try:
            return normalize_runtime_config(json.loads(target.read_text(encoding="utf-8"))), []
        except (OSError, json.JSONDecodeError, RuntimeConfigValidationError) as exc:
            return default_runtime_config(), [f"运行配置 {target} 加载失败，已使用默认值: {exc}"]
    config = default_runtime_config()
    legacy = Path(legacy_forecast_path)
    if legacy.exists():
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            prediction = replace(
                config.prediction,
                enabled_methods=tuple(raw.get("enabled_methods", config.prediction.enabled_methods)),
                enable_ensemble=bool(raw.get("enable_ensemble", config.prediction.enable_ensemble)),
            )
            config = normalize_runtime_config({"prediction": asdict(prediction)})
        except (OSError, json.JSONDecodeError, RuntimeConfigValidationError) as exc:
            return config, [f"旧预测配置 {legacy} 迁移失败，已使用默认值: {exc}"]
    return config, []


class RuntimeConfigStore:
    def __init__(self, config: RuntimeConfig):
        self._lock = threading.RLock()
        self._config = config

    def snapshot(self) -> RuntimeConfig:
        with self._lock:
            return self._config

    def replace(self, config: RuntimeConfig) -> None:
        if not isinstance(config, RuntimeConfig):
            raise TypeError("config must be RuntimeConfig")
        with self._lock:
            self._config = config

    def replace_payload(self, payload: Any) -> RuntimeConfig:
        config = normalize_runtime_config(payload)
        self.replace(config)
        return config


_initial_config, runtime_config_load_warnings = load_runtime_config()
runtime_config_store = RuntimeConfigStore(_initial_config)

