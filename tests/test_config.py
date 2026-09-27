import json
import logging

import pytest

from src.config import DEFAULT_PROBE_URLS, Config, ConfigError, load


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
