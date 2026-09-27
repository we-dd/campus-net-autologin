"""配置加载与校验。

设计要点：登录相关的三个端点 URL 全部可配置。
因为抓包尚未取得，默认值按锐捷 ePortal 的标准接口填写；
拿到抓包后若不符，改 config.json 即可，不需要动代码。

另一个要点：配置是用户手工编辑的（非程序员），所以这里所有可预期的错误
都必须变成 ConfigError + 中文说明，不能把 JSONDecodeError、UnicodeDecodeError、
ValueError 之类的原始异常抛到上层——上层只认 ConfigError，抛别的会变成
退出码 1 的崩溃堆栈，而不是该有的配置错误提示。
"""
from __future__ import annotations

import difflib
import json
import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PROBE_URLS: list[dict[str, Any]] = [
    {
        "url": "http://www.msftconnecttest.com/connecttest.txt",
        "expect_text": "Microsoft Connect Test",
    },
    {"url": "http://connect.rom.miui.com/generate_204", "expect_status": 204},
]

# 这里只有 log_level。username / portal 故意不放进来：它们要在下面单独处理
# （strip、查 http(s):// 前缀），一旦“顺手”把它们加进来去重，strip 和 scheme
# 校验就会一起失效，别做这个简化。
_STR_FIELDS = ("log_level",)
_INT_FIELDS = ("captcha_max_retry", "run_max_fail", "window_hours",
               "window_max_fail", "probe_timeout")
_OPTIONAL_URL_FIELDS = ("login_page_url", "login_submit_url", "captcha_url")

# 日志级别只认这五个。故意不用 logging.getLevelNamesMapping()：那张表里有 NOTSET
# （值是 0），填了它 root logger 的级别就变成 0，DEBUG 及以下会全部涌进 1 MiB 的
# 轮转日志；FATAL / WARN 这类别名也不放进来，用户没有理由手写它们。
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# bool 要在 int 前面判断：bool 是 int 的子类，否则 true 会被当成 1。
_TYPE_NAMES = ((bool, "布尔值"), (str, "字符串"), (int, "整数"),
               (float, "小数"), (list, "列表"), (dict, "对象"),
               (type(None), "空值"))


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


# 配置文件里允许出现的键，用于识别拼错的键（见 _warn_unknown_keys）。
# 直接从 Config 的字段推导：将来给 Config 加了字段却忘了同步这里，用户每次运行
# 都会收到一条假的“无法识别的配置项”警告，而那种错很难自己发现。
# 排除的这几个是 load() 按配置文件位置算出来的派生路径，不该写进配置文件。
_DERIVED_PATH_FIELDS = frozenset(
    {"base_dir", "secret_path", "state_path", "log_path", "samples_dir"}
)
_KNOWN_KEYS = frozenset(f.name for f in fields(Config)) - _DERIVED_PATH_FIELDS


def _type_name(value: Any) -> str:
    for typ, name in _TYPE_NAMES:
        if isinstance(value, typ):
            return name
    return type(value).__name__


def _show(value: Any) -> str:
    """把值渲染成给用户看的短字符串。"""
    text = f'"{value}"' if isinstance(value, str) else repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _bad_type(where: str, key: str, expected: str, value: Any) -> ConfigError:
    msg = f"{where} 字段 {key} 期望{expected}，实际是{_type_name(value)} {_show(value)}"
    if expected == "整数":
        if isinstance(value, str):
            msg += "（数字不要加引号）"
        elif isinstance(value, float):
            msg += "（去掉小数点）"
    return ConfigError(msg)


def _require_str(data: dict, key: str, where: str) -> str:
    if key not in data:
        raise ConfigError(f"{where} 缺少必填字段: {key}")
    value = data[key]
    if not isinstance(value, str):
        raise _bad_type(where, key, "字符串", value)
    return value


def _check_url(where: str, key: str, value: str, derivable: bool = False) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        msg = f"{where} 字段 {key} 必须是 http(s):// 开头的地址，实际是 {_show(value)}"
        if derivable:
            # 留空不再回退了，所以得告诉用户“想用默认值就别写这一项”
            msg += "（不写这一项就会按 portal 自动推导）"
        raise ConfigError(msg)
    return value


def _validate_probe_urls(urls: list, where: str) -> list[dict[str, Any]]:
    """逐条校验探测项，报错时指明是第几条（从 1 开始数）。"""
    if not urls:
        raise ConfigError(f"{where} 字段 probe_urls 不能为空列表")

    cleaned: list[dict[str, Any]] = []
    for index, entry in enumerate(urls, start=1):
        # 「字段」由 _bad_type 拼，这里不要再带一个，否则会出现“字段 xxx 字段 yyy”
        label = f"{where} probe_urls[{index}]"

        if not isinstance(entry, dict):
            raise ConfigError(
                f"{label} 必须是 JSON 对象（形如 "
                '{"url": "...", "expect_status": 204}），'
                f"实际是{_type_name(entry)} {_show(entry)}"
            )

        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(
                f"{label} 必须有 url，且是非空字符串，实际是 "
                f"{_type_name(url)} {_show(url)}"
            )

        has_text = "expect_text" in entry
        has_status = "expect_status" in entry
        if has_text and has_status:
            raise ConfigError(
                f"{label} 的 expect_text 和 expect_status 只能给一个，不能同时给"
            )
        if not has_text and not has_status:
            raise ConfigError(
                f"{label} 必须给 expect_text（期望页面里出现的文字）"
                f"或 expect_status（期望的 HTTP 状态码）之一"
            )

        item = dict(entry)
        item["url"] = url.strip()

        if has_text:
            text = entry["expect_text"]
            if not isinstance(text, str):
                raise _bad_type(label, "expect_text", "字符串", text)
            if not text.strip():
                # 空字符串在任何响应里都能匹配上，这条探测会永远判成“已在线”
                raise ConfigError(
                    f"{label} 的 expect_text 不能是空白，"
                    "空字符串在任何响应里都能匹配上，会让这条探测永远判成已在线"
                )
        else:
            status = entry["expect_status"]
            if isinstance(status, bool) or not isinstance(status, int):
                raise _bad_type(label, "expect_status", "整数", status)

        cleaned.append(item)

    return cleaned


def _warn_unknown_keys(data: dict, where: str) -> None:
    """拼错的键只警告、不报错。

    用户拿到抓包后要手改的就是这些键，写错了却按默认值跑，会让登录请求打到
    错误端点并一路累积失败，所以在日志里必须能看出来。
    """
    unknown = [key for key in data if key not in _KNOWN_KEYS]
    if not unknown:
        return

    known = sorted(_KNOWN_KEYS)
    for key in unknown:
        hint = difflib.get_close_matches(str(key), known, n=1)
        tail = f"，是不是想写 {hint[0]} ？" if hint else ""
        log.warning("%s 里有无法识别的配置项 %s%s（已忽略，按默认值处理）",
                    where, key, tail)


def load(path: str = "config.json") -> Config:
    if not os.path.exists(path):
        raise ConfigError(f"找不到配置文件: {path}")
    try:
        # utf-8-sig：记事本另存为 UTF-8 会带 BOM，用 utf-8 读会报一句开发者才看得懂的错。
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except UnicodeDecodeError as e:
        raise ConfigError(
            f"{path} 不是 UTF-8 编码，请用记事本打开，另存为时把编码选成 UTF-8: {e}"
        ) from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} 不是合法的 JSON: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层必须是 JSON 对象，实际是{_type_name(data)}")

    where = path

    username = _require_str(data, "username", where).strip()
    if not username:
        raise ConfigError(f"{where} 字段 username 不能为空")

    portal = _check_url(where, "portal", _require_str(data, "portal", where))
    portal = portal.rstrip("/")

    kwargs: dict[str, Any] = {"username": username, "portal": portal}

    for key in _INT_FIELDS:
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise _bad_type(where, key, "整数", value)
            if value <= 0:
                raise ConfigError(f"{where} 字段 {key} 必须大于 0，实际是 {value}")
            kwargs[key] = value

    for key in _STR_FIELDS:
        if key in data:
            kwargs[key] = _require_str(data, key, where)

    if "log_level" in kwargs:
        level_name = kwargs["log_level"].strip().upper()
        if level_name not in _LOG_LEVELS:
            raise ConfigError(
                f"{where} 字段 log_level 取值非法: {_show(kwargs['log_level'])}，"
                f"只能是 {' / '.join(_LOG_LEVELS)}"
            )
        kwargs["log_level"] = level_name

    if "probe_urls" in data:
        urls = data["probe_urls"]
        if not isinstance(urls, list):
            raise _bad_type(where, "probe_urls", "列表", urls)
        kwargs["probe_urls"] = _validate_probe_urls(urls, where)

    for key in _OPTIONAL_URL_FIELDS:
        if key in data:
            kwargs[key] = _check_url(
                where, key, _require_str(data, key, where), derivable=True
            )

    _warn_unknown_keys(data, where)

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
