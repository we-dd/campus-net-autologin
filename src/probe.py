"""网络状态探测。

认证成功与否的唯一可靠标志是"外网能不能正常出去"。
所以用外部探测地址 + 判定是否被重定向/返回 HTML 来判断，
而不是直接问 portal 服务器。
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any

import requests

from src.config import Config

log = logging.getLogger(__name__)


class State(Enum):
    """网络状态。只有三种。

    曾经还有个 UNKNOWN（"说不清，什么都别做"），已移除。它唯一会被触发的场景，
    恰好就是最该动手的场景：实测 AC 拦截时常用 200 + 不带 Content-Type 返回
    拦截内容，既不像劫持、也不匹配预期，于是被判成 UNKNOWN 而不登录 ——
    表现就是"开机后网络一直连不上，最后只能手动登"。

    一个"不确定所以不动手"的状态，如果只在需要行动的场合触发，比没有更糟。
    误判的代价由防锁号机制兜底（失败次数上限），而不是靠不动手来规避。
    """

    ONLINE = "online"            # 已联网，什么都不用做
    UNAUTHED = "unauthed"        # 在校园网内但外网不通，需要登录
    PORTAL_DOWN = "portal_down"  # 门户都摸不到，网络本身没通


def _matches(r, entry: dict[str, Any]) -> bool:
    if "expect_status" in entry:
        return r.status_code == entry["expect_status"]
    if "expect_text" in entry:
        return r.status_code == 200 and entry["expect_text"] in r.text
    return False


def _looks_hijacked(r) -> bool:
    if 300 <= r.status_code < 400:
        return True
    return "html" in r.headers.get("Content-Type", "").lower()


def _portal_reachable(cfg: Config, session) -> bool:
    try:
        session.get(cfg.portal, timeout=cfg.probe_timeout, allow_redirects=False)
        return True
    except requests.RequestException as e:
        log.debug("portal 不可达: %s", e)
        return False


def check(cfg: Config, session) -> State:
    any_hijack = False

    for entry in cfg.probe_urls:
        url = entry.get("url", "")
        try:
            r = session.get(url, timeout=cfg.probe_timeout, allow_redirects=False)
        except requests.RequestException as e:
            log.debug("探测地址不通 %s: %s", url, e)
            continue

        if _matches(r, entry):
            log.info("探测通过，已在线")
            return State.ONLINE
        if _looks_hijacked(r):
            any_hijack = True
            log.debug("探测地址 %s 疑似被劫持 (status=%s)", url, r.status_code)

    if not _portal_reachable(cfg, session):
        # 门户都摸不到 = 网络本身没通（刚开机还没拿到 IP、网线没插好、不在校园网）。
        # 这种时候登录也摸不到门户，跳过。
        log.info("门户不可达，网络本身没通")
        return State.PORTAL_DOWN

    # 门户可达 = 我们在校园网内，而外网没确认通 —— 这就是"未认证"。
    #
    # 判断依据是"门户是否可达"，不是"响应像不像劫持"。因为 AC 拦截时经常回
    # 200 + 不带 Content-Type，看起来既不像劫持也不匹配预期；只有拿门户可达性
    # 当依据才判得对。
    if any_hijack:
        log.info("判定为未认证（探测地址被重定向或返回 HTML）")
    else:
        log.info("判定为未认证（外网响应不符合预期）")
    return State.UNAUTHED
