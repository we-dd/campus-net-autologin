"""配置加载与校验。

设计要点：登录相关的三个端点 URL 全部可配置。
因为抓包尚未取得，默认值按锐捷 ePortal 的标准接口填写；
拿到抓包后若不符，改 config.json 即可，不需要动代码。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PROBE_URLS: list[dict[str, Any]] = [
    {
        "url": "http://www.msftconnecttest.com/connecttest.txt",
        "expect_text": "Microsoft Connect Test",
    },
    {"url": "http://connect.rom.miui.com/generate_204", "expect_status": 204},
]

_STR_FIELDS = ("username", "portal", "log_level")
_INT_FIELDS = ("captcha_max_retry", "run_max_fail", "window_hours",
               "window_max_fail", "probe_timeout")
_OPTIONAL_URL_FIELDS = ("login_page_url", "login_submit_url", "captcha_url")


class ConfigError(Exception):
    """配置文件缺失、格式错误或取值非法。"""


@dataclass
class Config:
    username: str
    portal: str

    captcha_max_retry: int = 5
    run_max_fail: int = 3
    window_hours: int = 6
    window_max_fail: int = 6
    probe_timeout: int = 5
    log_level: str = "INFO"
    probe_urls: list[dict[str, Any]] = field(default_factory=lambda: [dict(u) for u in DEFAULT_PROBE_URLS])

    login_page_url: str = ""
    login_submit_url: str = ""
    captcha_url: str = ""

    base_dir: str = "."
    secret_path: str = ""
    state_path: str = ""
    log_path: str = ""
    samples_dir: str = ""


def _require(data: dict, key: str, types, where: str):
    if key not in data:
        raise ConfigError(f"{where} 缺少必填字段: {key}")
    value = data[key]
    if not isinstance(value, types):
        raise ConfigError(f"{where} 字段 {key} 类型不对，期望 {types}，实际 {type(value).__name__}")
    return value


def load(path: str = "config.json") -> Config:
    if not os.path.exists(path):
        raise ConfigError(f"找不到配置文件: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} 不是合法的 JSON: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层必须是 JSON 对象")

    where = os.path.basename(path)

    username = _require(data, "username", str, where)
    if not username.strip():
        raise ConfigError(f"{where} 字段 username 不能为空")

    portal = _require(data, "portal", str, where)
    if not portal.startswith(("http://", "https://")):
        raise ConfigError(f"{where} 字段 portal 必须是 http(s):// 开头的地址，实际是 {portal!r}")

    portal = portal.rstrip("/")

    kwargs: dict[str, Any] = {"username": username.strip(), "portal": portal}

    for key in _INT_FIELDS:
        if key in data:
            value = _require(data, key, int, where)
            if value <= 0:
                raise ConfigError(f"{where} 字段 {key} 必须大于 0，实际是 {value}")
            kwargs[key] = value

    for key in _STR_FIELDS:
        if key in data and key not in kwargs:
            kwargs[key] = _require(data, key, str, where)

    if "probe_urls" in data:
        urls = _require(data, "probe_urls", list, where)
        if not urls:
            raise ConfigError(f"{where} 字段 probe_urls 不能为空列表")
        kwargs["probe_urls"] = [dict(u) for u in urls]

    for key in _OPTIONAL_URL_FIELDS:
        if key in data:
            kwargs[key] = _require(data, key, str, where)

    base_dir = os.path.dirname(os.path.abspath(path))
    cfg = Config(base_dir=base_dir, **kwargs)

    cfg.login_page_url = cfg.login_page_url or f"{portal}/eportal/InterFace.do?method=pageInfo"
    cfg.login_submit_url = cfg.login_submit_url or f"{portal}/eportal/InterFace.do?method=login"
    cfg.captcha_url = cfg.captcha_url or f"{portal}/eportal/validcode"

    cfg.secret_path = os.path.join(base_dir, "secret.dat")
    cfg.state_path = os.path.join(base_dir, "state.json")
    cfg.log_path = os.path.join(base_dir, "campus-net.log")
    cfg.samples_dir = os.path.join(base_dir, "samples")

    return cfg
