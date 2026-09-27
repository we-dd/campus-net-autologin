import os

import pytest

from src.state import State as FailState
from src.state import clear, is_stopped, load, prune, record_failure, save

HOUR = 3600


def test_load_missing_file_gives_empty_state(tmp_path):
    st = load(str(tmp_path / "nope.json"))
    assert st.failures == []
    assert st.stopped is False


def test_record_failure_appends_timestamp():
    st = FailState()
    record_failure(st, now=1000.0, window_hours=6, window_max_fail=3)
    assert st.failures == [1000.0]
    assert st.stopped is False


def test_stops_after_reaching_window_max_fail():
    st = FailState()
    for i in range(3):
        record_failure(st, now=1000.0 + i, window_hours=6, window_max_fail=3)
    assert st.stopped is True


def test_old_failures_expire_out_of_window():
    st = FailState()
    for i in range(3):
        record_failure(st, now=1000.0 + i, window_hours=6, window_max_fail=3)
    assert st.stopped is True
    # 从最后一次失败算起再过满 6 小时，记录才全部过期。
    # 基准是最后一次失败的时间（1000.0 + 2），不是第一次——按第一次算会少 2 秒，
    # 留下两条未过期的记录。
    prune(st, now=(1000.0 + 2) + 6 * HOUR + 1, window_hours=6)
    assert st.failures == []
    assert st.stopped is False


def test_partial_expiry_keeps_recent_failures():
    st = FailState()
    record_failure(st, now=0.0, window_hours=6, window_max_fail=99)
    record_failure(st, now=5 * HOUR, window_hours=6, window_max_fail=99)
    prune(st, now=6 * HOUR + 1, window_hours=6)
    assert st.failures == [5 * HOUR]


def test_clear_resets_everything():
    st = FailState()
    record_failure(st, now=0.0, window_hours=6, window_max_fail=1)
    assert st.stopped is True
    clear(st)
    assert st.failures == []
    assert st.stopped is False


def test_is_stopped_prunes_first():
    st = FailState()
    record_failure(st, now=0.0, window_hours=6, window_max_fail=1)
    assert is_stopped(st, now=1.0, window_hours=6) is True
    assert is_stopped(st, now=7 * HOUR, window_hours=6) is False


def test_save_and_load_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    st = FailState(failures=[1.0, 2.0], stopped=True)
    save(st, p)
    assert load(p) == st


def test_save_temp_name_is_per_process(tmp_path, monkeypatch):
    """临时文件名必须区分进程。

    固定的 path + ".tmp" 是所有进程共用的，手动跑一次撞上计划任务跑一次时两边会互相踩。
    这里把 os.getpid 换成两个不同的值来模拟两个进程各存一次，断言两边用的临时文件名不同，
    并确认成功路径上不留临时文件。
    """
    p = str(tmp_path / "state.json")
    used = []
    real_replace = os.replace

    def spy(src, dst):
        used.append(src)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    monkeypatch.setattr(os, "getpid", lambda: 1111)
    save(FailState(failures=[1.0]), p)
    monkeypatch.setattr(os, "getpid", lambda: 2222)
    save(FailState(failures=[2.0]), p)

    assert len(set(used)) == 2, f"两个进程共用了同一个临时文件: {used}"
    assert sorted(os.listdir(tmp_path)) == ["state.json"]


def test_corrupt_state_file_is_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{oops", encoding="utf-8")
    assert load(str(p)) == FailState()


@pytest.mark.parametrize("content", ["[]", '"abc"', "123", "null"])
def test_non_object_state_file_is_ignored(tmp_path, content):
    """顶层不是 JSON 对象的 state.json 也按空状态处理。

    真实场景：被停止自动登录后，用户会打开 state.json 想清空它，顺手写成 []。
    这时若抛 AttributeError，整个运行会崩掉，而不是按损坏状态降级。
    """
    p = tmp_path / "state.json"
    p.write_text(content, encoding="utf-8")
    assert load(str(p)) == FailState()
