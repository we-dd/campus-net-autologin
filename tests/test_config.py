import json
import logging

import pytest

from src.config import (
    DEFAULT_PROBE_URLS,
    SELECTOR_KEYS,
    Config,
    ConfigError,
    _KNOWN_KEYS,
    load,
)


def write(tmp_path, obj):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def minimal(**over):
    base = {"username": "2021001", "portal": "http://10.71.29.181"}
    base.update(over)
    return base


def test_load_applies_defaults(tmp_path):
    cfg = load(write(tmp_path, minimal()))
    assert cfg.username == "2021001"
    assert cfg.portal == "http://10.71.29.181"
    assert cfg.captcha_max_retry == 5
    assert cfg.run_max_fail == 3
    assert cfg.window_hours == 6
    assert cfg.window_max_fail == 6
    assert cfg.probe_timeout == 5
    assert cfg.log_level == "INFO"
    assert cfg.probe_urls == DEFAULT_PROBE_URLS


def test_default_urls_derive_from_portal(tmp_path):
    cfg = load(write(tmp_path, minimal()))
    assert cfg.login_page_url == "http://10.71.29.181/eportal/InterFace.do?method=pageInfo"
    assert cfg.login_submit_url == "http://10.71.29.181/eportal/InterFace.do?method=login"
    assert cfg.captcha_url == "http://10.71.29.181/eportal/validcode"


def test_explicit_urls_override_defaults(tmp_path):
    cfg = load(write(tmp_path, minimal(login_submit_url="http://x/submit")))
    assert cfg.login_submit_url == "http://x/submit"
    assert cfg.login_page_url == "http://10.71.29.181/eportal/InterFace.do?method=pageInfo"


def test_paths_resolve_next_to_config(tmp_path):
    cfg = load(write(tmp_path, minimal()))
    assert cfg.secret_path == str(tmp_path / "secret.dat")
    assert cfg.state_path == str(tmp_path / "state.json")
    assert cfg.log_path == str(tmp_path / "campus-net.log")


def test_probe_urls_are_not_shared_between_instances(tmp_path):
    a = load(write(tmp_path, minimal()))
    a.probe_urls.append({"url": "http://extra/"})
    b = load(write(tmp_path, minimal()))
    assert b.probe_urls == DEFAULT_PROBE_URLS


def test_missing_username_raises(tmp_path):
    with pytest.raises(ConfigError, match="username"):
        load(write(tmp_path, {"portal": "http://10.71.29.181"}))


def test_empty_username_raises(tmp_path):
    with pytest.raises(ConfigError, match="username"):
        load(write(tmp_path, minimal(username="  ")))


def test_portal_must_be_http_url(tmp_path):
    with pytest.raises(ConfigError, match="portal"):
        load(write(tmp_path, minimal(portal="10.71.29.181")))


def test_negative_retry_raises(tmp_path):
    with pytest.raises(ConfigError, match="captcha_max_retry"):
        load(write(tmp_path, minimal(captcha_max_retry=0)))


def test_wrong_type_raises(tmp_path):
    with pytest.raises(ConfigError, match="probe_timeout"):
        load(write(tmp_path, minimal(probe_timeout="5")))


def test_missing_file_raises_readable_error(tmp_path):
    with pytest.raises(ConfigError, match="找不到配置文件"):
        load(str(tmp_path / "nope.json"))


def test_bad_json_raises_readable_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法的 JSON"):
        load(str(p))


# --- 以下为评审补丁新增：probe_urls 形状校验 --------------------------------


@pytest.mark.parametrize(
    "bad_entry",
    [
        "http://x/",                                                      # 整条不是对象
        [5],                                                              # 整条不是对象
        5,                                                                # 整条不是对象
        {"expect_status": 204},                                           # 缺 url
        {"url": "   "},                                                   # url 是空白串
        {"url": "http://x/", "expect_text": "a", "expect_status": 204},   # 两者都给
        {"url": "http://x/"},                                             # 两者都没给
        {"url": "http://x/", "expect_status": "204"},                     # expect_status 类型不对
        {"url": "http://x/", "expect_status": True},                      # bool 不是整数
        {"url": "http://x/", "expect_text": 5},                           # expect_text 类型不对
    ],
)
def test_probe_urls_entry_shape_raises(tmp_path, bad_entry):
    with pytest.raises(ConfigError, match="probe_urls"):
        load(write(tmp_path, minimal(probe_urls=[bad_entry])))


def test_probe_urls_error_names_index(tmp_path):
    good = {"url": "http://ok/", "expect_status": 204}
    with pytest.raises(ConfigError, match=r"probe_urls\[2\]"):
        load(write(tmp_path, minimal(probe_urls=[good, {"nope": 1}])))


def test_probe_urls_missing_url_is_config_error_not_raw_exception(tmp_path):
    """缺 url 过去能加载成功，会让探测全部静默失败、误判成未登录。"""
    with pytest.raises(ConfigError, match="url"):
        load(write(tmp_path, minimal(probe_urls=[{"expect_status": 204}])))


def test_valid_probe_urls_are_kept(tmp_path):
    urls = [{"url": "http://ok/", "expect_status": 204, "note": "自定义键保留"}]
    cfg = load(write(tmp_path, minimal(probe_urls=urls)))
    assert cfg.probe_urls[0]["expect_status"] == 204
    assert cfg.probe_urls[0]["note"] == "自定义键保留"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_expect_text_raises(tmp_path, blank):
    """空字符串在任何响应里都能匹配上（"" in text 恒为真），会让这条探测永远
    判成已在线，工具就再也不登录了——所以必须拦住，而不是当成没写。"""
    with pytest.raises(ConfigError, match="expect_text"):
        load(write(tmp_path, minimal(
            probe_urls=[{"url": "http://x/", "expect_text": blank}]
        )))


# --- 以下为评审补丁新增：文件编码 --------------------------------------------


def test_non_utf8_file_raises_readable_error(tmp_path):
    p = tmp_path / "gbk.json"
    p.write_bytes(
        json.dumps(minimal(username="张三"), ensure_ascii=False).encode("gbk")
    )
    with pytest.raises(ConfigError, match="不是 UTF-8 编码"):
        load(str(p))


def test_utf8_bom_file_loads(tmp_path):
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(minimal()).encode("utf-8"))
    assert load(str(p)).username == "2021001"


# --- 以下为评审补丁新增：数值与 URL 取值 -------------------------------------


@pytest.mark.parametrize(
    "field",
    ["captcha_max_retry", "run_max_fail", "window_hours",
     "window_max_fail", "probe_timeout"],
)
def test_bool_for_int_field_raises(tmp_path, field):
    with pytest.raises(ConfigError, match=field):
        load(write(tmp_path, minimal(**{field: True})))


def test_float_for_int_field_says_drop_the_point(tmp_path):
    """写 2.5 秒是个很自然的手误，报错要给出可操作的说法，而不是只说类型不对。"""
    with pytest.raises(ConfigError, match="小数"):
        load(write(tmp_path, minimal(probe_timeout=2.5)))


def test_null_for_int_field_reads_as_blank_value(tmp_path):
    with pytest.raises(ConfigError, match="空值"):
        load(write(tmp_path, minimal(probe_timeout=None)))


def test_portal_is_stripped(tmp_path):
    cfg = load(write(tmp_path, minimal(portal="  http://10.71.29.181  ")))
    assert cfg.portal == "http://10.71.29.181"
    assert cfg.login_page_url == "http://10.71.29.181/eportal/InterFace.do?method=pageInfo"


@pytest.mark.parametrize(
    "field", ["login_page_url", "login_submit_url", "captcha_url"]
)
def test_url_fields_require_scheme(tmp_path, field):
    with pytest.raises(ConfigError, match=field):
        load(write(tmp_path, minimal(**{field: "10.71.29.181/eportal"})))


@pytest.mark.parametrize(
    "field", ["login_page_url", "login_submit_url", "captcha_url"]
)
def test_blank_url_field_error_explains_the_default(tmp_path, field):
    """留空的语义变了（过去悄悄回退成推导值，现在直接报错），所以报错里必须
    说清楚“想用默认值就别写这一项”，否则用户会卡在“那我要怎么用默认值”。"""
    with pytest.raises(ConfigError, match="自动推导"):
        load(write(tmp_path, minimal(**{field: ""})))


# --- 以下为评审补丁新增：log_level 与拼错的键 --------------------------------


def test_invalid_log_level_raises(tmp_path):
    with pytest.raises(ConfigError, match="log_level"):
        load(write(tmp_path, minimal(log_level="VERBOSE")))


@pytest.mark.parametrize("bad_level", ["NOTSET", "FATAL", "WARN", "notset"])
def test_disallowed_log_level_raises(tmp_path, bad_level):
    """NOTSET 在 getLevelNamesMapping() 里（值为 0），放过去会把 root 级别设成 0
    并让 DEBUG 及以下全部写进 1 MiB 的轮转日志，所以这里明确不认它。"""
    with pytest.raises(ConfigError, match="log_level"):
        load(write(tmp_path, minimal(log_level=bad_level)))


def test_lowercase_log_level_is_normalized(tmp_path):
    assert load(write(tmp_path, minimal(log_level="critical"))).log_level == "CRITICAL"


def test_unknown_key_warns_with_suggestion(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        cfg = load(write(tmp_path, minimal(captcha_max_retrys=7)))
    assert cfg.captcha_max_retry == 5
    assert "captcha_max_retrys" in caplog.text
    assert "是不是想写 captcha_max_retry" in caplog.text


def test_known_keys_do_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        load(write(tmp_path, minimal(log_level="DEBUG", probe_timeout=9)))
    assert caplog.text == ""


def test_known_keys_are_exactly_the_documented_options():
    """钉住这份名单。

    _KNOWN_KEYS 是从 Config 的字段推导的：将来给 Config 加了字段（或改了名）却
    没想清楚要不要让用户配，这里就会失败，逼着人来改一次——而不是让用户收到
    一条假的“无法识别的配置项”警告（那种错没人会注意到）。
    """
    assert _KNOWN_KEYS == {
        "username", "portal", "probe_urls", "log_level",
        "captcha_max_retry", "run_max_fail", "window_hours",
        "window_max_fail", "probe_timeout",
        "login_page_url", "login_submit_url", "captcha_url",
        "browser", "headless", "login_url", "selectors",
    }


# --- 以下为浏览器自动化（任务书变更）新增：browser / headless / login_url / selectors


def test_browser_defaults_to_edge(tmp_path):
    cfg = load(write(tmp_path, minimal()))
    assert cfg.browser == "edge"
    assert cfg.headless is False
    assert cfg.login_url == ""
    assert cfg.selectors == {}


@pytest.mark.parametrize("name", ["edge", "chrome"])
def test_browser_accepts_supported_values(tmp_path, name):
    assert load(write(tmp_path, minimal(browser=name))).browser == name


def test_browser_is_normalized_to_lowercase(tmp_path):
    assert load(write(tmp_path, minimal(browser="  Chrome "))).browser == "chrome"


@pytest.mark.parametrize("bad", ["firefox", "EDGE2", "msedge", ""])
def test_unsupported_browser_raises(tmp_path, bad):
    with pytest.raises(ConfigError, match="browser"):
        load(write(tmp_path, minimal(browser=bad)))


def test_headless_true_is_accepted(tmp_path):
    """headless 是本项目第一个 bool 配置项。

    现有的整数校验刻意拒绝 bool（bool 是 int 的子类），照抄那段逻辑会把
    headless: true 一起拒掉——这个用例就是钉住这件事。
    """
    assert load(write(tmp_path, minimal(headless=True))).headless is True


def test_headless_false_is_accepted(tmp_path):
    assert load(write(tmp_path, minimal(headless=False))).headless is False


@pytest.mark.parametrize("bad", ["yes", "no", "true", "false", "", 1, 0, None])
def test_headless_must_be_a_real_bool(tmp_path, bad):
    """字符串 "false" 是真值：如果按真值判断放过去，用户以为自己关掉了
    headless，实际上浏览器仍然不显示，而且没有任何提示。所以必须报错。"""
    with pytest.raises(ConfigError, match="headless"):
        load(write(tmp_path, minimal(headless=bad)))


def test_headless_string_error_tells_the_user_not_to_quote_it(tmp_path):
    with pytest.raises(ConfigError, match="引号"):
        load(write(tmp_path, minimal(headless="yes")))


def test_login_url_accepts_http_url(tmp_path):
    cfg = load(write(tmp_path, minimal(login_url="http://10.0.0.1/login")))
    assert cfg.login_url == "http://10.0.0.1/login"


def test_blank_login_url_means_discover_by_redirect(tmp_path):
    """留空是合法配置，不是错误：那时 browser_login 会打开探测地址，
    由门户把浏览器劫持到登录页去。"""
    assert load(write(tmp_path, minimal(login_url="   "))).login_url == ""


def test_login_url_requires_scheme(tmp_path):
    with pytest.raises(ConfigError, match="login_url"):
        load(write(tmp_path, minimal(login_url="10.0.0.1/login")))


def test_login_url_error_does_not_mention_derivation(tmp_path):
    """login_url 没有可推导的默认值，所以不能走 _OPTIONAL_URL_FIELDS：
    那句"不写这一项就会自动推导"会把人带偏。"""
    with pytest.raises(ConfigError, match="login_url") as e:
        load(write(tmp_path, minimal(login_url="nope")))
    assert "自动推导" not in str(e.value)


def test_selectors_accepts_string_map(tmp_path):
    cfg = load(write(tmp_path, minimal(selectors={"username": "#user", "submit": "#go"})))
    assert cfg.selectors == {"username": "#user", "submit": "#go"}


def test_selectors_values_are_stripped(tmp_path):
    cfg = load(write(tmp_path, minimal(selectors={"username": "  #user  "})))
    assert cfg.selectors == {"username": "#user"}


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-object",
        ["#user"],
        {"username": {"selector": "#user"}},   # 多包了一层对象，是很自然的误写
        {"username": 5},
        {"username": None},
    ],
)
def test_bad_selectors_raise_readable_error(tmp_path, bad):
    with pytest.raises(ConfigError, match="selectors"):
        load(write(tmp_path, minimal(selectors=bad)))


def test_selectors_object_is_not_shared_between_instances(tmp_path):
    a = load(write(tmp_path, minimal()))
    a.selectors["username"] = "#hacked"
    assert load(write(tmp_path, minimal())).selectors == {}


# --- 以下为真实门户校准新增：selectors 的键 --------------------------------

# 实测页面（四川工业科技学院 SAM SSO）验证过的一套选择器
REAL_PORTAL_SELECTORS = {
    "username": "#nameInput",
    "password": "input[type=password]",
    "captcha_input": "input[name=captcha_code]:not([type=hidden])",
    "captcha_image": "img.code-img",
    "captcha_reload": "a.code-reload-content",
    "agree": "input[type=checkbox]",
    "submit": "#submitBtn",
}


def test_all_real_portal_selectors_are_accepted(tmp_path):
    """这套值是拿真实页面校准过的，必须原样加载进来，一个键都不能被吞掉。"""
    assert load(write(tmp_path, minimal(selectors=REAL_PORTAL_SELECTORS))).selectors == \
        REAL_PORTAL_SELECTORS


def test_selector_keys_are_exactly_the_documented_options():
    """钉住名单。

    browser_login 只会读这几个键，config 只会校验这几个键——加新键（比如当初
    漏掉的 agree / captcha_reload）时必须两边一起改，这条会提醒。
    """
    assert SELECTOR_KEYS == (
        "username", "password", "captcha_input", "captcha_image",
        "captcha_reload", "agree", "submit",
    )


def test_unknown_selector_key_warns_with_a_suggestion(tmp_path, caplog):
    """写错键名（captca_input）不会报错，只会静默按自动识别跑，用户就一直
    "怎么又不灵了"——所以必须警告一次，并给出最接近的正确键名。"""
    with caplog.at_level(logging.WARNING, logger="src.config"):
        cfg = load(write(tmp_path, minimal(selectors={"captca_input": "#x"})))

    # 键保留下来（不报错、不吞掉），只是提醒它没人认
    assert cfg.selectors == {"captca_input": "#x"}
    assert "无法识别的键" in caplog.text
    assert "captcha_input" in caplog.text


def test_unknown_selector_key_without_a_near_match_still_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        load(write(tmp_path, minimal(selectors={"zzzzzz": "#x"})))
    assert "无法识别的键" in caplog.text


def test_known_selector_keys_do_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        load(write(tmp_path, minimal(selectors=REAL_PORTAL_SELECTORS)))
    assert caplog.text == ""


def test_selector_values_must_still_be_strings(tmp_path):
    """新键也要走同一条类型校验：写成对象是很自然的误写。"""
    with pytest.raises(ConfigError, match="selectors"):
        load(write(tmp_path, minimal(selectors={"agree": {"selector": "#x"}})))
