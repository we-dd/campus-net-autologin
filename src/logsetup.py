"""统一的日志初始化。

所有模块自带 logger，但不在模块内配置 handler，一律由这里配置根 logger。
因为脚本是被计划任务反复拉起的短命进程，这里每次运行都会重新配置；
但如果同一个进程里被调用多次，不能重复添加 handler，否则日志会重复。
"""
from __future__ import annotations

import logging
import logging.handlers
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_configured: set[str] = set()


def init(log_path: str, level: str = "INFO") -> None:
    key = os.path.abspath(log_path)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if key in _configured:
        return

    os.makedirs(os.path.dirname(key) or ".", exist_ok=True)

    fmt = logging.Formatter(_FORMAT)

    fh = logging.handlers.RotatingFileHandler(
        key, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    _configured.add(key)
