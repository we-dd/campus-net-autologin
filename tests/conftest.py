import logging
import sys
from pathlib import Path

import pytest

# 让 tests 里可以 `from src.xxx import yyy`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """每个用例跑完后，把 root logger 还原成本用例开始前的样子。

    logsetup.init() 把 handler 挂在 root logger 上，并把路径记在模块级的
    _configured 里，两者都活过整个 pytest 会话；而每个用例用的是新的 tmp_path。
    不还原的话 handler 会随用例数一路累积：日志重复写、旧临时文件被句柄占住删不掉，
    在 Windows 上就会变成难查的顺序相关失败。所以这段别当噪音删掉。
    """
    from src import logsetup

    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    configured_before = set(logsetup._configured)

    yield

    # 让用例之间互不影响：用过哪些日志路径也要还原
    logsetup._configured.clear()
    logsetup._configured.update(configured_before)

    # init() 会连 root 的级别一起改（Task 13 的 --verbose 就是传 DEBUG 进来的），
    # 级别不还原的话，后面的用例会一直按 DEBUG 往外写。
    root.setLevel(level_before)

    # 只清掉本用例新增的 handler，并 close 掉以释放文件句柄
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = handlers_before
