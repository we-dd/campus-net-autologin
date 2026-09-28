"""login.py 的编排：退出码，以及防锁号状态有没有被正确更新。

浏览器和网络都换成替身（monkeypatch），一个真实请求都不发、一个浏览器都不起。
"""
import json
import time

import login as cli
from src import browser_login, probe, state
from src.browser_login import Result
from src.config import load
from src.state import State


def make_cfg(tmp_path, **over):
    base = {"username": "2021001", "portal": "http://10.71.29.181"}
    base.update(over)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return load(str(path)), str(path)


def patch_net(monkeypatch, net_state):
    monkeypatch.setattr(probe, "check", lambda cfg, session: net_state)


def patch_login(monkeypatch, result, calls=None):
    def fake(cfg):
        if calls is not None:
            calls.append(cfg)
        return result

    monkeypatch.setattr(browser_login, "login", fake)


def write_state(cfg, failures=(), stopped=False):
    state.save(State(failures=list(failures), stopped=stopped), cfg.state_path)


# --- 探测结果决定要不要动浏览器 ---------------------------------------------


def test_check_exits_zero_and_keeps_the_browser_closed_when_online(tmp_path, monkeypatch):
    cfg, path = make_cfg(tmp_path)
    calls = []
    patch_net(monkeypatch, probe.State.ONLINE)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert calls == []


def test_check_clears_stale_state_once_online(tmp_path, monkeypatch):
    """人工登录一次之后，历史失败记录要跟着清掉，否则永远停在"停止自动登录"。"""
    cfg, path = make_cfg(tmp_path)
    write_state(cfg, failures=[time.time()], stopped=True)
    patch_net(monkeypatch, probe.State.ONLINE)
    patch_login(monkeypatch, Result(True, "success"))

    assert cli.main(["--check", "--config", path]) == 0
    st = state.load(cfg.state_path)
    assert st.failures == []
    assert st.stopped is False


def test_check_skips_login_when_portal_is_unreachable(tmp_path, monkeypatch):
    """portal 都摸不到时登录注定失败，而每次失败都在累积锁号风险。"""
    cfg, path = make_cfg(tmp_path)
    calls = []
    patch_net(monkeypatch, probe.State.PORTAL_DOWN)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert calls == []


def test_check_logs_in_when_portal_reachable_but_internet_is_not(tmp_path, monkeypatch):
    """门户可达 + 外网不通 → 去登录。

    这条守的是一个真实踩过的坑：AC 拦截时常用 200 + 不带 Content-Type 返回
    拦截内容，旧逻辑会判成"说不清"然后什么都不做，表现就是开机后网络一直
    连不上，最后只能手动登。现在这种状态就是 UNAUTHED。
    """
    cfg, path = make_cfg(tmp_path)
    calls = []
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert len(calls) == 1


def test_check_backs_off_after_a_recent_failure(tmp_path, monkeypatch):
    """失败之后短时间内不再重试。

    轮询间隔调短（5 分钟）是为了"网一掉就尽快恢复"，但如果登录一直在失败，
    短间隔就变成密集地打认证服务器 —— 6 次失败半小时内攒完，反而更容易撞上
    学校风控。退避让探测保持灵敏，同时把登录尝试的密度限住。
    """
    cfg, path = make_cfg(tmp_path)
    now = 1_000_000.0
    write_state(cfg, failures=[now - 60])          # 1 分钟前失败过
    calls = []
    monkeypatch.setattr(cli.time, "time", lambda: now)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert calls == []


def test_check_retries_once_the_backoff_has_passed(tmp_path, monkeypatch):
    """过了退避时间就正常重试 —— 别把退避写成永久停止。"""
    cfg, path = make_cfg(tmp_path)
    now = 1_000_000.0
    write_state(cfg, failures=[now - (cfg.retry_backoff_minutes * 60 + 60)])
    calls = []
    monkeypatch.setattr(cli.time, "time", lambda: now)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert len(calls) == 1


def test_backoff_does_not_block_the_already_online_shortcut(tmp_path, monkeypatch):
    """已经在线时不受退避影响 —— 该清的失败计数还是要清。"""
    cfg, path = make_cfg(tmp_path)
    now = 1_000_000.0
    write_state(cfg, failures=[now - 60])
    calls = []
    monkeypatch.setattr(cli.time, "time", lambda: now)
    patch_net(monkeypatch, probe.State.ONLINE)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert state.load(cfg.state_path).failures == []


def test_backoff_does_not_block_the_lockout_stop_message(tmp_path, monkeypatch):
    """已进入"停止自动登录"状态时，仍然要弹提示 —— 退避不能把它遮掉。"""
    cfg, path = make_cfg(tmp_path)
    now = 1_000_000.0
    write_state(cfg, failures=[now - 60], stopped=True)
    calls = []
    monkeypatch.setattr(cli.time, "time", lambda: now)
    monkeypatch.setattr(cli, "_notify", lambda *a, **k: None)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 1
    assert calls == []


# --- 防锁号护栏 -------------------------------------------------------------


def test_check_stops_before_touching_the_network_when_guard_is_tripped(tmp_path, monkeypatch):
    cfg, path = make_cfg(tmp_path)
    write_state(cfg, failures=[time.time()], stopped=True)
    calls = []
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 1
    assert calls == []


# --- 真的去登录 -------------------------------------------------------------


def test_check_logs_in_when_unauthenticated(tmp_path, monkeypatch):
    cfg, path = make_cfg(tmp_path)
    calls = []
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success", "登录表单已从页面上消失"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert len(calls) == 1


def test_successful_login_clears_the_failure_history(tmp_path, monkeypatch):
    cfg, path = make_cfg(tmp_path)
    # 时间要往前推过退避期：这条测的是"登录成功后清空失败记录"，
    # 写一个刚刚失败过的时间会被退避挡住，根本走不到登录那一步。
    write_state(cfg, failures=[time.time() - (cfg.retry_backoff_minutes * 60 + 60)])
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success"))

    assert cli.main(["--check", "--config", path]) == 0
    assert state.load(cfg.state_path).failures == []


def test_failed_login_is_recorded_as_a_failure(tmp_path, monkeypatch):
    cfg, path = make_cfg(tmp_path)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(False, "captcha_failed", "验证码识别结果为空"))

    assert cli.main(["--check", "--config", path]) == 1
    assert len(state.load(cfg.state_path).failures) == 1


def test_bad_credentials_is_recorded_and_returns_one(tmp_path, monkeypatch):
    cfg, path = make_cfg(tmp_path)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(False, "bad_credentials", "页面提示「密码错误」"))

    assert cli.main(["--check", "--config", path]) == 1
    assert len(state.load(cfg.state_path).failures) == 1


def test_unreadable_password_file_is_not_counted_as_a_login_failure(tmp_path, monkeypatch):
    """密码文件读不出来时根本没发出登录请求。

    计进锁号窗口会触发"账号要被锁了"的假警报，还会把人工登录才该解除的
    stopped 状态提前点亮，所以这里必须退出 2 且一条失败都不记。
    """
    cfg, path = make_cfg(tmp_path)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(False, "secret", "找不到密码文件"))

    assert cli.main(["--check", "--config", path]) == 2
    assert state.load(cfg.state_path).failures == []


def test_login_ignores_the_stop_guard_because_it_is_manual(tmp_path, monkeypatch):
    """--login 是用户手动敲的，目的就是强制试一次；护栏留给无人值守的 --check。"""
    cfg, path = make_cfg(tmp_path)
    write_state(cfg, failures=[time.time()], stopped=True)
    calls = []
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--login", "--config", path]) == 0
    assert len(calls) == 1


# --- 参数与环境 -------------------------------------------------------------


def test_main_exits_two_on_config_error(tmp_path, capsys):
    assert cli.main(["--check", "--config", str(tmp_path / "nope.json")]) == 2
    assert "配置错误" in capsys.readouterr().err


def test_main_returns_two_when_no_action_is_given(tmp_path, capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_dump_form_returns_one_when_the_page_cannot_be_opened(tmp_path, monkeypatch, capsys):
    cfg, path = make_cfg(tmp_path)
    monkeypatch.setattr(browser_login, "dump_form",
                        lambda cfg: Result(False, "browser_error", "起不来"))

    assert cli.main(["--dump-form", "--config", path]) == 1
    assert "起不来" in capsys.readouterr().out
