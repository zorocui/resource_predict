"""应用级 logging 初始化：文件 + 控制台（可配置）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from resource_predict.settings import settings

_done = False


def setup_application_logging() -> None:
    """幂等：多次调用只配置一次。根 logger 级别与 handler 由 settings.app 控制。"""
    global _done
    if _done:
        return
    _done = True

    app_cfg = settings.app
    level_name = (app_cfg.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, date_fmt)

    root = logging.getLogger()
    root.setLevel(level)

    if app_cfg.log_console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(level)
        ch.setFormatter(formatter)
        root.addHandler(ch)

    log_name = app_cfg.log_file
    if log_name:
        log_name = str(log_name).strip()
    if log_name:
        out_dir = Path(app_cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / log_name
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(formatter)
        root.addHandler(fh)
