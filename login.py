#!/usr/bin/env python
"""校园网自动认证 CLI。

用法：
  python login.py --check          供计划任务调用：先探测，没网就登录
  python login.py --login          强制登录一次，跳过探测（调试用）
  python login.py --setup          交互式保存密码
  python login.py --dump-form      打开登录页，打印页面上的输入框/图片/按钮

退出码：0 已在线或登录成功；1 登录失败；2 配置或环境错误。
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import time

import requests

from src import browser_login, logsetup, probe, secret, state
from src.config import ConfigError, load

log = logging.getLogger(__name__)


def _notify(title: str, message: str) -> None:
    """通知用户。

    弹窗（src/notify.py）按任务书的要求没有做，所以这里一律走日志 + stderr。
    其中真正有意义的是日志那一条：计划任务跑起来时 stderr 是被丢掉的，
    只有日志文件里能事后查到到底发生了什么。
    """
    log.error("%s: %s", title, message)
    print(f"{title}: {message}", file=sys.stderr)


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    return s


def do_login(cfg, st, now: float) -> int:
    """调一次浏览器登录，并按结果更新状态文件。

    重试次数由 browser_login 内部按 cfg.captcha_max_retry 封顶，
    所以这里只调一次，不做外层重试——外层再套一层循环就等于把那个上限翻倍了。
    """
    res = browser_login.login(cfg)

    if res.ok:
        log.info("登录成功: %s", res.detail)
        state.clear(st)
        state.save(st, cfg.state_path)
        return 0

    log.error("登录失败 (%s): %s", res.reason, res.detail)

    if res.reason == "secret":
        # 本地密码文件读不出来，**根本没有发出登录请求**。所以不计入锁号窗口
        # （计了会触发"账号要被锁了"的假警报），按环境错误退出 2。
        _notify("校园网认证", f"本地密码文件不可用，无法自动登录。\n{res.detail}")
        return 2

    if res.reason == "bad_credentials":
        # 确定性失败：再试没有意义，只会加速锁号。但它确实消耗了锁号预算，要计入。
        _notify("校园网认证", f"账号或密码错误，已停止重试。\n{res.detail}")
        state.record_failure(st, now, cfg.window_hours, cfg.window_max_fail)
        state.save(st, cfg.state_path)
        return 1

    state.record_failure(st, now, cfg.window_hours, cfg.window_max_fail)
    state.save(st, cfg.state_path)
    return 1


def run_check(cfg) -> int:
    """计划任务走的路径：探测 → 必要时登录。"""
    now = time.time()
    st = state.load(cfg.state_path)
    state.prune(st, now, cfg.window_hours)

    with _new_session() as session:
        net = probe.check(cfg, session)

    if net is probe.State.ONLINE:
        state.clear(st)
        state.save(st, cfg.state_path)
        log.info("已在线，无需操作")
        return 0

    if net in (probe.State.PORTAL_DOWN, probe.State.UNKNOWN):
        # 这两种情况登录注定失败，而每次失败都在累积锁号风险，不能白试
        log.warning("网络层问题（%s），跳过登录", net.value)
        return 0

    if state.is_stopped(st, now, cfg.window_hours):
        _notify(
            "校园网认证",
            "连续失败次数过多，已暂停自动登录，避免账号被锁。\n"
            "请手动登录一次校园网；确认账号正常后，也可以删掉 state.json 解除。",
        )
        return 1

    log.info("检测到未认证，开始登录")
    return do_login(cfg, st, now)


def run_login(cfg) -> int:
    """--login：跳过探测，强制登录一次。

    这里刻意不拦 state 里的 stopped 标志：--login 是用户坐在机器前面手动敲的，
    目的正是在"探测说在线、实际却掉线"这类情况下强制试一次。会无人值守反复重试的
    入口是 --check，那里必须拦——防锁号的护栏留给它才是有意义的。
    """
    now = time.time()
    st = state.load(cfg.state_path)
    if state.is_stopped(st, now, cfg.window_hours):
        log.warning("当前处于停止自动登录状态；--login 是手动强制登录，继续执行")
    return do_login(cfg, st, now)


def run_setup(cfg) -> int:
    print(f"配置文件: {cfg.base_dir}")
    username = input(f"学号/账号 [{cfg.username}]: ").strip() or cfg.username
    password = getpass.getpass("密码（输入时不显示）: ")
    if not password:
        print("密码为空，已取消。", file=sys.stderr)
        return 2
    secret.save_password(password, cfg.secret_path)
    print(f"\n账号: {username}")
    print(f"密码已加密保存到: {cfg.secret_path}")
    if username != cfg.username:
        # 这里改的只是密码文件，config.json 里的 username 还是旧的。不说清楚的话，
        # 用户会以为改完了，然后一直用一个不存在的账号登录，直到账号被锁。
        print(f"\n注意：配置文件里的 username 是 {cfg.username}，请把它改成 {username}")
    return 0


def run_dump_form(cfg) -> int:
    res = browser_login.dump_form(cfg)
    print(res.detail)
    return 0 if res.ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="login.py", description="校园网自动认证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="探测并按需登录，供计划任务调用")
    parser.add_argument("--login", action="store_true", help="强制登录一次，跳过探测")
    parser.add_argument("--setup", action="store_true", help="交互式保存密码")
    parser.add_argument("--dump-form", action="store_true",
                        help="打开登录页并打印页面元素，用于校准 selectors")
    parser.add_argument("--config", default=None,
                        help="配置文件路径，默认脚本同目录下的 config.json")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")

    args = parser.parse_args(argv)

    if not any([args.check, args.login, args.setup, args.dump_form]):
        parser.print_help()
        return 2

    cfg_path = args.config or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"
    )

    # 先按配置文件所在目录初始化日志，这样连"配置文件读不出来"这种错也能落盘。
    # 计划任务跑起来时 stderr 是被丢掉的，不先开日志就等于什么都没留下。
    # 正常情况下 cfg.log_path 与这个路径相同，下面第二次 init 会被去重短路掉。
    fallback_log = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "campus-net.log")

    # 这次 init 也要包在 try 里：日志目录不可写（只读盘、权限、路径某段是文件）
    # 同样会抛 OSError，那不该变成栈回溯和退出码 1。
    try:
        logsetup.init(fallback_log, "DEBUG" if args.verbose else "INFO")
        cfg = load(cfg_path)
    except (ConfigError, OSError) as e:
        log.error("配置错误: %s", e)
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    logsetup.init(cfg.log_path, "DEBUG" if args.verbose else cfg.log_level)
    log.info("=== 启动: %s ===", " ".join(sys.argv[1:]))

    try:
        if args.setup:
            return run_setup(cfg)
        if args.dump_form:
            return run_dump_form(cfg)
        if args.login:
            return run_login(cfg)
        return run_check(cfg)
    except Exception:
        log.exception("未预期的错误")
        return 2


if __name__ == "__main__":
    sys.exit(main())
