import json

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
