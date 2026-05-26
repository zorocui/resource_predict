from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from resource_predict.settings import settings


SUPPORTED_FORECAST_METHODS: tuple[dict[str, str], ...] = (
    {"key": "arima", "label": "ARIMA"},
    {"key": "sarima", "label": "SARIMA"},
    {"key": "prophet", "label": "Prophet"},
    {"key": "seasonal_naive", "label": "Seasonal naive"},
    {"key": "rolling_mean", "label": "Rolling mean"},
)
DEFAULT_FORECAST_CONFIG_PATH = Path("deploy") / "forecast_config.json"


class ForecastConfigValidationError(ValueError):
    pass


def supported_method_keys() -> set[str]:
    return {item["key"] for item in SUPPORTED_FORECAST_METHODS}


def default_forecast_config_payload() -> Dict[str, Any]:
    return {
        "enabled_methods": list(settings.forecast.enabled_methods),
        "enable_ensemble": bool(settings.forecast.enable_ensemble),
    }


def normalize_forecast_config_payload(payload: Any) -> Dict[str, Any]:
    if payload is None:
        payload = default_forecast_config_payload()
    if not isinstance(payload, dict):
        raise ForecastConfigValidationError("request body must be a JSON object")

    supported = supported_method_keys()
    raw_methods = payload.get("enabled_methods", settings.forecast.enabled_methods)
    if not isinstance(raw_methods, list):
        raise ForecastConfigValidationError("enabled_methods must be a list")

    enabled_methods: List[str] = []
    for raw in raw_methods:
        method = str(raw).strip()
        if not method:
            continue
        if method not in supported:
            raise ForecastConfigValidationError(f"unsupported forecast method: {method}")
        if method not in enabled_methods:
            enabled_methods.append(method)
    if not enabled_methods:
        raise ForecastConfigValidationError("at least one forecast method must be enabled")

    return {
        "enabled_methods": enabled_methods,
        "enable_ensemble": bool(payload.get("enable_ensemble", settings.forecast.enable_ensemble)),
    }


def read_forecast_config(path: Path | str = DEFAULT_FORECAST_CONFIG_PATH) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return normalize_forecast_config_payload(default_forecast_config_payload())
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ForecastConfigValidationError(f"{p} is not valid JSON: {exc}") from exc
    return normalize_forecast_config_payload(payload)


def write_forecast_config(
    payload: Any,
    path: Path | str = DEFAULT_FORECAST_CONFIG_PATH,
) -> Dict[str, Any]:
    normalized = normalize_forecast_config_payload(payload)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return normalized


def read_forecast_config_payload(path: Path | str = DEFAULT_FORECAST_CONFIG_PATH) -> Dict[str, Any]:
    config = read_forecast_config(path)
    return {
        "supported_methods": list(SUPPORTED_FORECAST_METHODS),
        **config,
    }
