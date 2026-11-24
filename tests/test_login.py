"""login.py 的编排：退出码，以及防锁号状态有没有被正确更新。

浏览器和网络都换成替身（monkeypatch），一个真实请求都不发、一个浏览器都不起。
"""
import json
import time

import pytest

import login as cli
from src import browser_login, driver_update, probe, state
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


def patch_net_seq(monkeypatch, states):
    """按序列返回探测结果；序列用完之后一直返回最后一个。

    给"门户不可达 → 重探 → 门户就绪"这类场景用。
    """
    it = iter(states)

    def fake(cfg, session):
        return next(it, states[-1])

    monkeypatch.setattr(probe, "check", fake)


def patch_login(monkeypatch, result, calls=None):
    def fake(cfg):
        if calls is not None:
            calls.append(cfg)
        return result

    monkeypatch.setattr(browser_login, "login", fake)


def write_state(cfg, failures=(), stopped=False):
    state.save(State(failures=list(failures), stopped=stopped), cfg.state_path)


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """把"门户不可达"重探的间隔清零。

    本项目的测试从不 patch time.sleep，而是把间隔常量置 0（browser_login 的
    _SETTLE_STEP 就是这个约定）。不清零的话，每条走到 PORTAL_DOWN 的用例都要
    真睡 portal_wait_probes × _PORTAL_REPROBE_STEP = 12 × 5 秒。
    """
    monkeypatch.setattr(cli, "_PORTAL_REPROBE_STEP", 0)


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


# --- 门户不可达时先重探几次，别立刻放弃 ---------------------------------------


def test_check_waits_for_the_portal_instead_of_giving_up_immediately(tmp_path, monkeypatch):
    """门户不可达有两种原因：不在校园网，和刚开机网卡还没就绪。

    2026-10-15 实测栽在第二种：12:09:45 判"门户不可达"直接退出，而**4 秒后
    门户就可达了**，那次机会白丢。所以现在要重探几次。
    """
    cfg, path = make_cfg(tmp_path)
    calls = []
    patch_net_seq(monkeypatch, [
        probe.State.PORTAL_DOWN,
        probe.State.PORTAL_DOWN,
        probe.State.UNAUTHED,
    ])
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert len(calls) == 1, "门户就绪之后应该接着去登录"


def test_check_gives_up_when_the_portal_never_comes_up(tmp_path, monkeypatch):
    """真不在校园网时，重探完还是要干脆地跳过 —— 别去碰认证服务器。"""
    cfg, path = make_cfg(tmp_path)
    calls = []
    patch_net_seq(monkeypatch, [probe.State.PORTAL_DOWN])
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert calls == []


def test_check_finishes_online_when_the_network_comes_up_mid_wait(tmp_path, monkeypatch):
    """重探期间探到已在线，必须走"已在线"分支，不能掉进登录。"""
    cfg, path = make_cfg(tmp_path)
    write_state(cfg, failures=[time.time()])
    calls = []
    patch_net_seq(monkeypatch, [probe.State.PORTAL_DOWN, probe.State.ONLINE])
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 0
    assert calls == []
    assert state.load(cfg.state_path).failures == []


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

    退出码必须是 1 而不是 0：这一轮之所以不动手，是因为上一次登录失败了、网络
    到现在还没通。报 0 会让计划任务把它记成成功，把上一轮的失败覆盖掉。
    """
    cfg, path = make_cfg(tmp_path)
    now = 1_000_000.0
    write_state(cfg, failures=[now - 60])          # 1 分钟前失败过
    calls = []
    monkeypatch.setattr(cli.time, "time", lambda: now)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--check", "--config", path]) == 1
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


def test_failed_login_is_success_when_a_reprobe_finds_the_network_up(
    tmp_path, monkeypatch,
):
    """开机时探测早于网络就绪，登录失败不代表真的没网。

    2026-10-17 12:24 开机实测：第一次探测判未认证，浏览器起来后网络已经
    自己通了，于是打开探测地址找不到登录表单。复探以"外网通不通"为准。
    """
    cfg, path = make_cfg(tmp_path)
    patch_net_seq(monkeypatch, [probe.State.UNAUTHED, probe.State.ONLINE])
    patch_login(monkeypatch, Result(False, "not_login_page", "没找到登录表单"))

    assert cli.main(["--check", "--config", path]) == 0
    assert state.load(cfg.state_path).failures == []


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


def test_timeout_is_not_counted_as_a_login_failure(tmp_path, monkeypatch):
    """认证页没加载出来时一个凭据都没提交，不该占用锁号预算。

    依据见 browser_login._attempt_loop：nav_timed_out 只传给第 1 次尝试，而它
    一旦放弃整轮循环就返回，所以 timeout 严格等价于"没打到认证服务器" ——
    和上面 secret 那条同类。

    不记进去还有一层实际意义：记了就会触发 retry_backoff_minutes（默认 15 分钟）
    的退避，而"刚开机时门户加载慢"这种瞬时情况恰恰等不起 15 分钟。
    """
    cfg, path = make_cfg(tmp_path)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(False, "timeout", "页面上共 0 个表单元素"))

    assert cli.main(["--check", "--config", path]) == 1
    assert state.load(cfg.state_path).failures == []


def test_network_unstable_is_not_counted_as_a_login_failure(tmp_path, monkeypatch):
    """浏览器停在网络错误页时一个凭据都没提交，不该占用锁号预算、也不该退避。

    2026-10-28 21:00 实测：笔记本唤醒后网卡抖了约 15 秒，浏览器正好在抖动窗口
    里发起导航，被 ERR_NETWORK_CHANGED 掐断。网络 14 秒后就恢复了，可这一轮被
    判成失败并进入 15 分钟退避 —— 一次一秒钟的抖动换来整轮放弃。
    """
    cfg, path = make_cfg(tmp_path)
    patch_net(monkeypatch, probe.State.UNAUTHED)
    patch_login(monkeypatch, Result(False, "network_unstable", "ERR_NETWORK_CHANGED"))

    assert cli.main(["--check", "--config", path]) == 1
    assert state.load(cfg.state_path).failures == []


def test_login_ignores_the_stop_guard_because_it_is_manual(tmp_path, monkeypatch):
    """--login 是用户手动敲的，目的就是强制试一次；护栏留给无人值守的 --check。"""
    cfg, path = make_cfg(tmp_path)
    write_state(cfg, failures=[time.time()], stopped=True)
    calls = []
    patch_login(monkeypatch, Result(True, "success"), calls)

    assert cli.main(["--login", "--config", path]) == 0
    assert len(calls) == 1


# --- 驱动自动更新 -----------------------------------------------------------


def test_update_driver_exits_zero_on_success(tmp_path, monkeypatch, capsys):
    cfg, path = make_cfg(tmp_path)
    monkeypatch.setattr(
        driver_update, "update_if_needed",
        lambda cfg, session=None, force=False: driver_update.UpdateResult(
            True, "已更新：Chrome 157.0.8103.80 → 157.0.8103.78", changed=True,
        ),
    )

    assert cli.main(["--update-driver", "--config", path]) == 0
    assert "已更新" in capsys.readouterr().out


def test_update_driver_exits_one_on_failure(tmp_path, monkeypatch, capsys):
    cfg, path = make_cfg(tmp_path)
    monkeypatch.setattr(
        driver_update, "update_if_needed",
        lambda cfg, session=None, force=False: driver_update.UpdateResult(
            False, "下载失败：所有源都不可用",
        ),
    )

    assert cli.main(["--update-driver", "--config", path]) == 1
    assert "下载失败" in capsys.readouterr().err


def test_a_broken_driver_update_never_breaks_the_online_verdict(tmp_path, monkeypatch):
    """驱动更新只是顺手做的事：它炸了，也不能把"已在线"变成失败。"""
    cfg, path = make_cfg(tmp_path)
    patch_net(monkeypatch, probe.State.ONLINE)

    def boom(*args, **kwargs):
        raise RuntimeError("网络库炸了")

    monkeypatch.setattr(driver_update, "update_if_needed", boom)

    assert cli.main(["--check", "--config", path]) == 0


def test_auto_update_can_be_switched_off(tmp_path, monkeypatch):
    """config.json 里 auto_update_driver=false 时，检查回合不该去碰驱动。"""
    cfg, path = make_cfg(tmp_path, auto_update_driver=False)
    patch_net(monkeypatch, probe.State.ONLINE)
    calls = []
    monkeypatch.setattr(
        driver_update, "update_if_needed",
        lambda *a, **k: calls.append(1) or driver_update.UpdateResult(True, "ok"),
    )

    assert cli.main(["--check", "--config", path]) == 0
    assert calls == []


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
