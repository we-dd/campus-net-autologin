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


def test_corrupt_state_file_is_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{oops", encoding="utf-8")
    assert load(str(p)) == FailState()
