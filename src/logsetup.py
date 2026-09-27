"""统一的日志初始化。

所有模块自带 logger，但不在模块内配置 handler，一律由这里配置根 logger。
因为脚本是被计划任务反复拉起的短命进程，这里每次运行都会重新配置；
但如果同一个进程里被调用多次，不能重复添加 handler，否则日志会重复。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_configured: set[str] = set()


def init(log_path: str, level: str = "INFO") -> None:
    real_path = os.path.abspath(log_path)
    # 用 normcase 后的路径去重：Windows 上 a.log 和 A.LOG 是同一个文件，
    # 只用 abspath 会得到两个 key、两个 handler，日志写两遍。
    key = os.path.normcase(real_path)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if key in _configured:
        return

    os.makedirs(os.path.dirname(real_path) or ".", exist_ok=True)

    fmt = logging.Formatter(_FORMAT)

    fh = logging.handlers.RotatingFileHandler(
        real_path, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 计划任务里用 pythonw.exe 跑（不弹黑窗口），此时 sys.stderr 是 None。
    # StreamHandler 拿不到 stream，写第一条日志就会抛 AttributeError。
    # 没有控制台就只写文件。
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _configured.add(key)
