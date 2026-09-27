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


def test_init_does_not_truncate_existing_log(tmp_path):
    log_file = tmp_path / "c.log"
    log_file.write_text("上一轮的记录\n", encoding="utf-8")
    init(str(log_file), "INFO")
    logging.getLogger("src.x").info("本轮")
    for h in logging.getLogger().handlers:
        h.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "上一轮的记录" in content and "本轮" in content
