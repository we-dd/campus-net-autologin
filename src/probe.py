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
    ONLINE = "online"            # 已联网，什么都不用做
    UNAUTHED = "unauthed"        # 被 portal 劫持，需要登录
    PORTAL_DOWN = "portal_down"  # portal 服务器都摸不到，网络层故障
    UNKNOWN = "unknown"          # 外网不通，但也没看到劫持特征


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
    any_response = False
    any_hijack = False

    for entry in cfg.probe_urls:
        url = entry.get("url", "")
        try:
            r = session.get(url, timeout=cfg.probe_timeout, allow_redirects=False)
        except requests.RequestException as e:
            log.debug("探测地址不通 %s: %s", url, e)
            continue

        any_response = True
        if _matches(r, entry):
            log.info("探测通过，已在线")
            return State.ONLINE
        if _looks_hijacked(r):
            any_hijack = True
            log.debug("探测地址 %s 疑似被劫持 (status=%s)", url, r.status_code)

    if not _portal_reachable(cfg, session):
        return State.PORTAL_DOWN

    if any_hijack or not any_response:
        return State.UNAUTHED

    return State.UNKNOWN
