#!/usr/bin/env python
"""校园网自动认证 CLI。

用法：
  python login.py --check          供计划任务调用：先探测，没网就登录
  python login.py --login          强制登录一次，跳过探测（调试用）
  python login.py --setup          交互式保存密码
  python login.py --dump-form      打开登录页，打印页面上的输入框/图片/按钮

退出码：0 已在线或登录成功；1 未能上网（登录失败，或仍在失败退避窗口内）；
        2 配置或环境错误。
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import time

import requests

from src import browser_login, driver_update, logsetup, probe, secret, state
from src.config import ConfigError, load

log = logging.getLogger(__name__)

# 两次"门户不可达"重探之间的间隔秒数（重探几次由 cfg.portal_wait_probes 决定）。
# 5 秒是实测出来的：唤醒后 Wi-Fi 通常几秒内就绪，15 秒会让整个登录白白晚 10 秒；
# 再往下压（1-3 秒）收益很小，反而在真正断网时多打几次 HTTP。
# 测试里置 0：本项目的测试从不 patch time.sleep，而是把这类间隔常量清零
# （browser_login 的 _SETTLE_STEP 就是这个约定）。
_PORTAL_REPROBE_STEP = 5.0


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
    # 绕开系统代理。
    # requests 在 Windows 上会去读注册表里的系统代理设置（IE/WinINET 那一套），
    # 而这台机器上开着本地代理（127.0.0.1:7897，Clash 之类）。
    # 我们要判断的是"校园网认没认证"，不是"代理通不通"。
    # 走代理的话，校园网断线时探测会被代理吞掉，判定结果不再反映真实网络状态。
    s.trust_env = False
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    return s


def _probe(cfg) -> probe.State:
    """探测一次。每次都用新 session，和原来的写法一致。"""
    with _new_session() as session:
        return probe.check(cfg, session)


def _probe_until_portal_ready(cfg) -> probe.State:
    """探到"门户不可达"时，隔一会儿再探几次，而不是立刻放弃。

    "门户不可达"有两种截然不同的原因：不在校园网（没插网线 / 没连 SCITWiFi），
    和刚开机 / 刚唤醒（网卡还没就绪）。原来的代码一律当成前者，直接 return 0
    什么都不做。2026-10-15 开机实测就是这么栽的：12:09:45 判"门户不可达"，
    4 秒后门户就可达了 —— 那次机会白白丢掉了。

    重探几乎不花钱：探测只发 HTTP、不起浏览器，实测一次门户不可达的整轮只要 56ms。
    真不在校园网时，代价也只是几次探测加几段等待，不会碰认证服务器。
    """
    net = _probe(cfg)
    if net is not probe.State.PORTAL_DOWN:
        return net

    for i in range(1, cfg.portal_wait_probes + 1):
        time.sleep(_PORTAL_REPROBE_STEP)
        net = _probe(cfg)
        if net is not probe.State.PORTAL_DOWN:
            log.info("门户已就绪（第 %d 次重探）", i)
            return net

    log.warning("门户仍然不可达（已重探 %d 次）", cfg.portal_wait_probes)
    return net


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

    # 失败后立刻复探一次再下结论。
    #
    # 从探测到浏览器真正打开登录页，中间隔着 Chrome 启动和页面加载，开机时这段
    # 可能有几十秒。2026-10-17 12:24 开机实测：探测时外网还不通，等浏览器起来时
    # 网络已经自己恢复了；浏览器打开探测地址当然找不到登录表单，这次运行就被判成
    # not_login_page 并记进退避窗口，而机器其实已经在线。
    # 本项目的唯一成功标准是"外网能不能出去"（见 src/probe.py），所以以复探为准。
    if _probe(cfg) is probe.State.ONLINE:
        log.warning(
            "登录未完成 (%s)，但复探确认网络已通，按成功处理: %s",
            res.reason, res.detail,
        )
        state.clear(st)
        state.save(st, cfg.state_path)
        return 0

    log.error("登录失败 (%s): %s", res.reason, res.detail)

    if res.reason == "secret":
        # 本地密码文件读不出来，**根本没有发出登录请求**。所以不计入锁号窗口
        # （计了会触发"账号要被锁了"的假警报），按环境错误退出 2。
        _notify("校园网认证", f"本地密码文件不可用，无法自动登录。\n{res.detail}")
        return 2

    if res.reason == "timeout":
        # 认证页压根没加载出来（表单没找到），这一轮**一个凭据都没提交**。
        # 依据见 browser_login._attempt_loop：nav_timed_out 只传给第 1 次尝试，
        # 而第 1 次一放弃整个循环就返回，所以这条 reason 严格等价于
        # "没有打到认证服务器" —— 和上面的 secret 同类。
        #
        # 因此不计入锁号窗口，也不触发退避：退避是按"失败过"来压重试密度的，
        # 而这里根本没有失败可言。真记进去的话，一次页面加载慢就会把接下来
        # 15 分钟的重试全部挡掉 —— 开机的场景恰恰等不起这 15 分钟。
        #
        # 退出码仍给 1：这次运行确实没连上网，该如实上报。
        _notify("校园网认证", f"认证页没加载出来，没有提交任何凭据。\n{res.detail}")
        return 1

    if res.reason == "network_unstable":
        # 浏览器停在它自己的网络错误页上（典型场景：刚唤醒，网卡还在反复重连），
        # 这一轮**一个凭据都没提交** —— 和上面的 timeout 同类。
        # 不计入锁号窗口，也不触发退避：一次一秒钟的抖动不该把接下来 15 分钟的
        # 重试全挡掉（2026-10-28 实测就是这么被挡掉的）。退出码照样给 1。
        _notify("校园网认证", f"页面停在浏览器的网络错误页，没有提交任何凭据。\n{res.detail}")
        return 1

    if res.reason == "bad_credentials":
        # 确定性失败：再试没有意义，只会加速锁号。但它确实消耗了锁号预算，要计入。
        _notify("校园网认证", f"账号或密码错误，已停止重试。\n{res.detail}")
        state.record_failure(st, now, cfg.window_hours, cfg.window_max_fail)
        state.save(st, cfg.state_path)
        return 1

    if res.reason == "no_network_after_login":
        # 页面说成功、网络却没通。这不能算成功 —— 算成功就会清空失败计数，
        # 下一轮又发现没网、又登一次，形成每 5 分钟一次的循环（2026-09-28 实测）。
        # 计入失败，让退避把重试密度压下来。
        _notify("校园网认证", f"页面显示登录成功，但网络仍未通。\n{res.detail}")
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

    net = _probe_until_portal_ready(cfg)

    if net is probe.State.ONLINE:
        state.clear(st)
        state.save(st, cfg.state_path)
        log.info("已在线，无需操作")
        _maybe_update_driver(cfg)
        return 0

    if net is probe.State.PORTAL_DOWN:
        # 门户都摸不到，登录也摸不到，跳过。
        # 注意这里以前还包含一个 UNKNOWN 状态（"说不清就什么都别做"），
        # 已随 probe 一起移除：实测它会在"其实该登录"的时候触发，导致开机后
        # 一直不恢复。宁可多试一次（有防锁号兜底），也不要该动手时不动手。
        log.warning("门户不可达，跳过登录")
        return 0

    if state.is_stopped(st, now, cfg.window_hours):
        _notify(
            "校园网认证",
            "连续失败次数过多，已暂停自动登录，避免账号被锁。\n"
            "请手动登录一次校园网；确认账号正常后，也可以删掉 state.json 解除。",
        )
        return 1

    # 退避：失败之后不要立刻再试。
    #
    # 计划任务的轮询间隔调短是为了"网一掉就尽快恢复"，但如果登录一直在失败，
    # 短间隔就变成密集地打认证服务器 —— 6 次失败半小时内攒完，反而更容易撞上
    # 学校风控。退避让"探测"保持灵敏，同时把"登录尝试"的密度限住。
    if st.failures:
        since = now - max(st.failures)
        cool = cfg.retry_backoff_minutes * 60
        if since < cool:
            log.warning(
                "距上次登录失败仅 %.0f 秒（退避 %d 分钟），本轮不重试",
                since,
                cfg.retry_backoff_minutes,
            )
            # 这里返回 1 而不是 0。
            #
            # 本轮确实什么都没做，但"什么都没做"的原因是：上一次登录失败了，而网络
            # 到现在还没通。返回 0 等于告诉计划任务"这轮一切正常"，会把上一轮的失败
            # 从任务列表里覆盖掉。2026-10-24 的驱动版本故障就是这么被藏了将近 4 个
            # 小时的：任务状态一直显示成功（最后结果是 15:40:06 那轮退避跳过的 0），
            # 真正的 ERROR 在 15:40:02 那次运行里，已经被下一轮盖掉了。
            return 1

    log.info("检测到未认证，开始登录")
    # "网络真的通了没通"由 browser_login 在**浏览器还开着**的时候确认
    # （见 _confirm_network_up）—— 关早了会把门户开闸的请求掐断。这里不再复探。
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


def run_update_driver(cfg) -> int:
    """--update-driver：把本地驱动对齐到本机 Chrome。

    手动入口，所以 force=True：用户既然敲了这条命令，就不要拿"冷却中"挡他。
    """
    res = driver_update.update_if_needed(cfg, force=True)
    if res.ok:
        print(res.detail)
        return 0
    print(res.detail, file=sys.stderr)
    return 1


def _maybe_update_driver(cfg) -> None:
    """已经在线时，顺手把驱动对齐到本机 Chrome 的版本。

    时机是刻意选的：这一轮已经确认在线，下载走的是真实外网，不缺网、也不和
    登录抢时间。反过来，真需要登录的那些轮次恰恰是没网的时候，那时下载必然失败
    （2026-10-23 就是卡在这个死结上）。

    任何异常都只记日志：驱动更新失败绝不能影响"这一轮已经在线"这个结论，
    更不能把 run_check 的返回值改掉。
    """
    if not getattr(cfg, "auto_update_driver", True):
        return
    try:
        res = driver_update.update_if_needed(cfg)
    except Exception:
        log.exception("检查驱动版本时出错（忽略）")
        return
    if res.changed:
        log.info("驱动已自动更新：%s", res.detail)
    elif not res.ok:
        log.warning("驱动自动更新未完成：%s", res.detail)


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
    parser.add_argument("--update-driver", action="store_true",
                        help="把本地 chromedriver 更新到与本机 Chrome 匹配的版本")
    parser.add_argument("--config", default=None,
                        help="配置文件路径，默认脚本同目录下的 config.json")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")

    args = parser.parse_args(argv)

    if not any([args.check, args.login, args.setup, args.dump_form, args.update_driver]):
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
        if args.update_driver:
            return run_update_driver(cfg)
        if args.login:
            return run_login(cfg)
        return run_check(cfg)
    except Exception:
        log.exception("未预期的错误")
        return 2


if __name__ == "__main__":
    sys.exit(main())
