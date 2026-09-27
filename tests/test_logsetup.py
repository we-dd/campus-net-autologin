import logging
import logging.handlers

import pytest

from src.logsetup import init


def test_init_creates_log_file(tmp_path):
    log_file = tmp_path / "x.log"
    init(str(log_file), "INFO")
    logging.getLogger("src.probe").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_file.exists()
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_init_does_not_duplicate_handlers(tmp_path):
    init(str(tmp_path / "a.log"), "INFO")
    n = len(logging.getLogger().handlers)
    init(str(tmp_path / "a.log"), "INFO")
    assert len(logging.getLogger().handlers) == n


def test_init_does_not_truncate_existing_log(tmp_path):
    log_file = tmp_path / "c.log"
    log_file.write_text("上一轮的记录\n", encoding="utf-8")
    init(str(log_file), "INFO")
    logging.getLogger("src.x").info("本轮")
    for h in logging.getLogger().handlers:
        h.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "上一轮的记录" in content and "本轮" in content


@pytest.fixture(scope="module")
def shared_log(tmp_path_factory):
    """下面两个用例共用同一个日志路径：第二个要验证“同一路径能再次 init”。"""
    return str(tmp_path_factory.mktemp("shared") / "shared.log")


def test_init_attaches_handlers(shared_log):
    before = len(logging.getLogger().handlers)
    init(shared_log, "INFO")
    assert len(logging.getLogger().handlers) == before + 2


def test_conftest_cleanup_is_not_cosmetic(shared_log):
    """依赖上一个用例：它用同一个路径调过 init()。

    1) 那个用例挂上去的 handler 必须已被 conftest 的 fixture 摘掉，否则会累积；
    2) _configured 里的路径也必须一并还原，否则这里的 init() 会被短路、一个
       handler 都不加。第 2 条是关键：只摘 handler 不还原路径，清理就是装样子的，
       而这条断言正是为了不让以后有人把那段 fixture 当噪音删掉。
    """
    leftovers = [h for h in logging.getLogger().handlers
                 if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert leftovers == []

    before = len(logging.getLogger().handlers)
    init(shared_log, "INFO")
    assert len(logging.getLogger().handlers) == before + 2


# 导入时（收集阶段，还没有用例跑过）先记下 root 的原始级别
_LEVEL_AT_IMPORT = logging.getLogger().level


def test_init_sets_root_level(tmp_path):
    init(str(tmp_path / "d.log"), "DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_root_level_is_restored():
    """级别也是会话级状态：init() 会改 root 级别，conftest 的 fixture 必须还原。

    上一个用例把级别设成了 DEBUG；fixture 若不还原，这里看到的就是 DEBUG，
    而不是导入时的原始级别。单独跑本用例时它是空转的——它是个泄漏探测器，
    靠的是同文件内的定义顺序（放最后），不是自身有多严格。
    """
    assert logging.getLogger().level == _LEVEL_AT_IMPORT
