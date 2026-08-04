"""应用启动设置。

业务运行配置由 Web 页面管理并保存到 deploy/runtime_config.json；算法与存储实现默认值
位于 internal_settings.py，不属于用户配置面。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BootstrapSettings:
    static_folder: str = "static"
    template_folder: str = "templates"
    out_dir: str = "outputs"
    log_file: Optional[str] = "resource_predict.log"
    log_level: str = "INFO"
    log_console: bool = True
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False


bootstrap_settings = BootstrapSettings()

# 过渡期内部 API：现有业务模块读取它时，运行字段来自线程安全快照。
from resource_predict import internal_settings as _internal_settings  # noqa: E402

AppConfig = _internal_settings.AppConfig
GenerationConfig = _internal_settings.GenerationConfig
settings = _internal_settings.settings
