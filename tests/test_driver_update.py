"""驱动自动更新：版本判断、下载源兜底、安装、冷却。

一个真实请求都不发、一个真实浏览器都不起：HTTP 换成 tests/fakes 里的替身，
缓存目录换成 tmp_path。
"""
import io
import json
import os
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import browser_login, driver_update
from tests.fakes import FakeResponse, FakeSession


def driver_zip(payload=b"MZ this is a fake chromedriver"):
    """造一个结构正确的驱动压缩包（CfT 的目录布局）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("chromedriver-win64/chromedriver.exe", payload)
    return buf.getvalue()


def milestone_json(version="157.0.8103.78"):
    return json.dumps({"milestones": {"157": {"version": version}}})


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(browser="chrome", base_dir=str(tmp_path))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """把缓存根指到 tmp_path，别碰开发机上真实的 Selenium 缓存。"""
    root = tmp_path / "selenium-cache"
    monkeypatch.setattr(driver_update, "cache_root", lambda: root)
    return root


# --- 判断 -------------------------------------------------------------------


def test_check_says_ok_when_a_matching_driver_is_cached(cfg, monkeypatch):
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(
        driver_update, "_cached_versions", lambda b: ["156.0.8078.47", "157.0.8103.78"],
    )

    status = driver_update.check(cfg)

    assert status.needs_update is False
    assert "157.0.8103.78" in status.detail


def test_check_flags_a_stale_cache(cfg, monkeypatch):
    """Chrome 已经 157，缓存里只有 156 —— 这就是 2026-11-24 的现场。"""
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: ["156.0.8078.47"])

    status = driver_update.check(cfg)

    assert status.needs_update is True
    assert status.chrome_version == "157.0.8103.80"
    assert "156.0.8078.47" in status.detail


def test_check_skips_edge(cfg):
    """Edge 的驱动来自另一套分发体系，宁可不做，也不要瞎下。"""
    cfg.browser = "edge"

    status = driver_update.check(cfg)

    assert status.needs_update is False
    assert "Chrome" in status.detail


def test_check_survives_an_unknown_browser_version(cfg, monkeypatch):
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: None)

    status = driver_update.check(cfg)

    assert status.needs_update is False
    assert "查不到" in status.detail


# --- 下载与安装 --------------------------------------------------------------


def test_update_downloads_and_installs_the_matching_driver(cfg, cache, monkeypatch):
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    session = FakeSession([
        ("https://registry.npmmirror.com/", FakeResponse(200, content=driver_zip())),
    ])

    res = driver_update.update_if_needed(cfg, session=session)

    assert res.ok is True
    assert res.changed is True
    dest = cache / "chromedriver" / "win64" / "157.0.8103.80" / "chromedriver.exe"
    assert dest.exists()
    assert dest.read_bytes().startswith(b"MZ")
    # 落盘用的 .part 不该留下
    assert not (dest.parent / "chromedriver.exe.part").exists()


def test_update_falls_back_to_the_latest_build_of_the_major(cfg, cache, monkeypatch):
    """本机构建号在镜像和官方源都没有时（灰度推送），退回该主版本的最新构建。"""
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    session = FakeSession([
        ("https://registry.npmmirror.com/-/binary/chrome-for-testing/157.0.8103.80/",
         FakeResponse(404)),
        ("https://googlechromelabs.github.io/", FakeResponse(200, text=milestone_json())),
        ("https://registry.npmmirror.com/-/binary/chrome-for-testing/157.0.8103.78/",
         FakeResponse(404)),
        ("https://storage.googleapis.com/chrome-for-testing-public/157.0.8103.80/",
         FakeResponse(404)),
        ("https://storage.googleapis.com/chrome-for-testing-public/157.0.8103.78/",
         FakeResponse(200, content=driver_zip())),
    ])

    res = driver_update.update_if_needed(cfg, session=session)

    assert res.ok is True
    assert (cache / "chromedriver" / "win64" / "157.0.8103.78" / "chromedriver.exe").exists()


def test_a_bogus_payload_is_never_installed(cfg, cache, monkeypatch):
    """下载到 HTML 错误页时必须在落盘前拦住，否则登录会开始报奇怪的错。"""
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    session = FakeSession([
        ("https://registry.npmmirror.com/",
         FakeResponse(200, content=driver_zip(payload=b"<html>404</html>"))),
    ])

    res = driver_update.update_if_needed(cfg, session=session)

    assert res.ok is False
    assert "MZ" in res.detail
    # 目录可以建（下次安装要用），但绝不能留下一个可执行文件
    assert not list((cache / "chromedriver").rglob("chromedriver.exe"))


# --- 冷却 -------------------------------------------------------------------


def test_a_failed_download_starts_a_cooldown(cfg, cache, monkeypatch):
    """下载失败后不能每 5 分钟再来一次：那会把 --check 拖成几分钟一轮。"""
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    down = FakeSession([("https://", FakeResponse(404))])

    first = driver_update.update_if_needed(cfg, session=down)
    second = driver_update.update_if_needed(cfg, session=FakeSession([("https://", FakeResponse(404))]))

    assert first.ok is False
    assert second.ok is False
    assert "冷却" in second.detail


def test_force_ignores_the_cooldown(cfg, cache, monkeypatch):
    """手动 --update-driver 不该被冷却挡住。"""
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    driver_update.update_if_needed(
        cfg, session=FakeSession([("https://", FakeResponse(404))]),
    )

    res = driver_update.update_if_needed(
        cfg,
        session=FakeSession([
            ("https://registry.npmmirror.com/", FakeResponse(200, content=driver_zip())),
        ]),
        force=True,
    )

    assert res.ok is True
    assert res.changed is True


def test_cooldown_is_per_chrome_version(cfg, cache, monkeypatch):
    """Chrome 又升级了就该重新试，别被上一次的失败压着。"""
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    driver_update.update_if_needed(
        cfg, session=FakeSession([("https://", FakeResponse(404))]),
    )

    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "158.0.1.1")
    status = driver_update.check(cfg)

    assert status.needs_update is True
    assert driver_update._cooldown_left(cfg, "158.0.1.1") == 0.0


# --- 并发锁 -----------------------------------------------------------------


def test_a_second_update_is_blocked_by_the_lock(cfg, monkeypatch):
    """计划任务和手动命令同时跑时，别下两份 11 MB（2026-11-24 实测发生过）。"""
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    Path(cfg.base_dir, "driver-update.lock").write_text("12345")

    res = driver_update.update_if_needed(cfg, session=FakeSession([]))

    assert res.ok is False
    assert "另一个进程" in res.detail


def test_a_stale_lock_is_taken_over(cfg, cache, monkeypatch):
    """进程被强杀留下的锁不该永久挡住更新。"""
    monkeypatch.setattr(browser_login, "_browser_version", lambda b: "157.0.8103.80")
    monkeypatch.setattr(driver_update, "_cached_versions", lambda b: [])
    lock = Path(cfg.base_dir, "driver-update.lock")
    lock.write_text("12345")
    old = time.time() - 2 * driver_update._LOCK_STALE_SECONDS
    os.utime(lock, (old, old))

    res = driver_update.update_if_needed(
        cfg,
        session=FakeSession([
            ("https://registry.npmmirror.com/", FakeResponse(200, content=driver_zip())),
        ]),
    )

    assert res.ok is True
    assert not lock.exists()          # 用完就释放
