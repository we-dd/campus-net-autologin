import logging

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


def test_init_appends_file_handler_only_once_per_path(tmp_path):
    log_file = tmp_path / "b.log"
    init(str(log_file), "DEBUG")
    logging.getLogger("src.x").debug("second-run")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "second-run" in log_file.read_text(encoding="utf-8")
