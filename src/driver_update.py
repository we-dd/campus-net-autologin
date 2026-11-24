"""把本地 chromedriver 对齐到本机 Chrome 的版本。

背景：Chrome 大约每四周自动升级一次，Selenium 缓存里的驱动就落后一个主版本。
实测（2026-11-24）差一个主版本通常还能用，跨两个就会 session not created；
真正难受的是**时机** —— 能下载驱动的时候往往已经在线、脚本压根不启动浏览器，
等真需要登录时又没网，于是永远补不上（2026-10-23 就这么坏了一整天）。

所以这里只做一件事：判断"缓存里有没有和本机 Chrome 主版本一致的驱动"，
没有就去 Chrome for Testing 下一个，校验后放进 Selenium 的缓存目录。
什么时候调用由 login.py 决定：只在**已经确认在线**的那一轮顺手做。

几个刻意的选择：

  1. 只处理 Chrome。Edge 的驱动来自另一套分发体系（msedgedriver / EdgeUpdate），
     和 Chrome for Testing 完全不是一回事，混在一起写容易两头都写不好。
  2. 下载源三层兜底：先精确到构建号，再退回"该主版本的最新构建"，最后是
     国内镜像（storage.googleapis.com 在校园网里不总是通）。
  3. 失败要冷却。下载失败往往意味着"网被卡住了"，不冷却的话每一轮 --check
     都会挂几分钟等超时，比不更新还糟。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from src import browser_login

log = logging.getLogger(__name__)

# Chrome for Testing 的官方分发点。构建号（157.0.8103.80）能直接从路径里取。
_CFT_BASE = "https://storage.googleapis.com/chrome-for-testing-public"
# 只列"每个主版本的最新构建"，比 known-good-versions 小一个数量级（100 KB 对 6 MB）。
_CFT_MILESTONES = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "latest-versions-per-milestone-with-downloads.json"
)
# 国内镜像：同一个目录结构，实测可用。
_NPM_MIRROR = "https://registry.npmmirror.com/-/binary/chrome-for-testing"

# 失败之后多久不再重试。6 小时足够跨过一次网络故障，又不会让"驱动一直补不上"
# 这件事被永久藏起来。
_COOLDOWN_HOURS = 6

# 连接要快（10 秒连不上就换源）；单次读取也不能太久（60 秒没动静就当断了）。
#
# 但**单次读取超时挡不住"细水长流"**：2026-11-24 实测，从 storage.googleapis.com
# 拉 11 MB 的包，每个 socket 读都在超时内返回，累计却挂了 7 分 53 秒毫无结果。
# 所以还要有**总时长**上限，而且分源给：镜像通常几秒，官方源在校园网里很慢。
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 60
_JSON_READ_TIMEOUT = 20
_MIRROR_SECONDS = 30
_OFFICIAL_SECONDS = 60
_TOTAL_SECONDS = 300

_MARKER_NAME = "driver-update.json"
_LOCK_NAME = "driver-update.lock"
# 锁超过这个年龄就当成上一个进程崩溃后留下的，直接接管。
_LOCK_STALE_SECONDS = 900


@dataclass
class Status:
    """一次"要不要更新"的判断结果。"""
    needs_update: bool
    detail: str
    chrome_version: str = ""
    driver_version: str = ""


@dataclass
class UpdateResult:
    ok: bool
    detail: str
    changed: bool = False


def _platform_id(browser: str) -> str | None:
    """Chrome for Testing 目录名里的平台标识；无法自动更新时返回 None。"""
    if browser != "chrome":
        return None
    if os.name != "nt":
        return None
    return "win64" if sys.maxsize > 2 ** 32 else "win32"


def cache_root() -> Path:
    """Selenium Manager 的缓存根目录（和 browser_login._cached_driver_path 一致）。"""
    return Path.home() / ".cache" / "selenium"


def _cached_versions(browser: str) -> list[str]:
    """缓存里已有的驱动版本号列表，从旧到新。"""
    spec = browser_login._DRIVER_CACHE.get(browser)
    if not spec:
        return []
    folder, exe = spec
    try:
        found = {p.parent.name for p in (cache_root() / folder).glob(f"*/*/{exe}")}
    except OSError:
        return []
    return sorted(found, key=browser_login._version_key)


def check(cfg) -> Status:
    """只判断，不下载、不改文件。"""
    browser = getattr(cfg, "browser", "chrome")
    if _platform_id(browser) is None:
        return Status(False, f"browser={browser}：自动更新只覆盖 Windows 上的 Chrome")

    chrome = browser_login._browser_version(browser)
    if not chrome:
        return Status(False, "查不到本机 Chrome 的版本号，跳过驱动检查（不影响登录）")

    major = browser_login._major_from_text(chrome)
    versions = _cached_versions(browser)
    matched = [v for v in versions if browser_login._major_from_text(v) == major]
    if matched:
        return Status(
            False,
            f"驱动已匹配：Chrome {chrome}，缓存中 {matched[-1]}",
            chrome_version=chrome,
            driver_version=matched[-1],
        )
    return Status(
        True,
        f"驱动没跟上：Chrome {chrome}，缓存里只有 "
        f"{'、'.join(versions) if versions else '（空）'}",
        chrome_version=chrome,
    )


# --- 下载 -------------------------------------------------------------------


def _try_get(session, url: str, read_timeout: float) -> bytes | None:
    """取一个小文件（版本表之类的 JSON）；失败只记日志、返回 None。"""
    try:
        resp = session.get(url, timeout=(_CONNECT_TIMEOUT, read_timeout))
    except requests.RequestException as e:
        log.info("取 %s 失败：%s", url, e)
        return None
    if resp.status_code != 200:
        log.info("%s 返回 HTTP %d", url, resp.status_code)
        return None
    return resp.content or None


def _download(session, url: str, deadline: float, source_seconds: float) -> bytes | None:
    """下载驱动包，受 deadline 和 source_seconds 双重约束。

    刻意不用 resp.content 一把梭：那样只能靠 socket 读超时兜底，而慢速连接每次读
    都有进展，读超时永远不会触发 —— 这正是 2026-11-24 卡 7 分 53 秒的原因。
    这里按 chunk 累加，每个 chunk 之后检查一次总时长。
    """
    stop_at = min(deadline, time.monotonic() + source_seconds)
    if stop_at <= time.monotonic():
        return None

    try:
        resp = session.get(url, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT), stream=True)
    except requests.RequestException as e:
        log.info("取 %s 失败：%s", url, e)
        return None

    try:
        if resp.status_code != 200:
            log.info("%s 返回 HTTP %d", url, resp.status_code)
            return None
        chunks: list[bytes] = []
        got = 0
        next_note = 4 * 1024 * 1024
        for chunk in resp.iter_content(256 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            got += len(chunk)
            if got >= next_note:
                log.info("已下载 %.1f MB（%s）", got / 1024 / 1024, url)
                next_note += 4 * 1024 * 1024
            if time.monotonic() > stop_at:
                log.warning(
                    "下载 %s 超时（已下 %.1f MB），换下一个源",
                    url, got / 1024 / 1024,
                )
                return None
        return b"".join(chunks) or None
    except requests.RequestException as e:
        log.info("下载 %s 中断：%s", url, e)
        return None
    finally:
        try:
            resp.close()
        except Exception:  # 关不掉也不影响结果
            pass


def _zip_url(base: str, version: str, platform_id: str) -> str:
    return f"{base}/{version}/{platform_id}/chromedriver-{platform_id}.zip"


def _milestone_version(session, major: int) -> str | None:
    """该主版本在 Chrome for Testing 里的最新构建号。"""
    data = _try_get(session, _CFT_MILESTONES, _JSON_READ_TIMEOUT)
    if not data:
        return None
    try:
        parsed = json.loads(data.decode("utf-8", "replace"))
        version = ((parsed.get("milestones") or {}).get(str(major)) or {}).get("version")
    except (ValueError, AttributeError) as e:
        log.info("解析里程碑版本表失败：%s", e)
        return None
    return version if isinstance(version, str) and version else None


def _fetch_zip(session, chrome_version: str, major: int, platform_id: str):
    """拿到驱动压缩包。返回 (版本号, 字节)；所有源都不通时返回 None。

    顺序是按实测速度排的，不是按"权威程度"排的：

      1. 镜像 + 本机构建号   —— 命中时约 2 秒（实测 6.8 MB/s）
      2. 镜像 + 该主版本最新构建 —— 本机构建号往往还没同步到镜像（2026-11-24：
         157.0.8103.80 在镜像上是 404，157.0.8103.78 秒下）；差一个补丁版对
         ChromeDriver 没有影响，它只认主版本
      3. 官方源 + 两种版本   —— 权威但慢，实测约 50 KB/s，11 MB 要 4 分钟

    每层都校验 MZ 头和 `--version` 输出，主版本对不上就不装。
    """
    deadline = time.monotonic() + _TOTAL_SECONDS
    tried: set[str] = set()

    def attempt(version: str, base: str, seconds: float):
        url = _zip_url(base, version, platform_id)
        if url in tried:
            return None
        tried.add(url)
        data = _download(session, url, deadline, seconds)
        if data:
            log.info("下载成功：%s（%.1f MB）", url, len(data) / 1024 / 1024)
            return version, data
        return None

    found = attempt(chrome_version, _NPM_MIRROR, _MIRROR_SECONDS)
    if found:
        return found

    # 镜像同步会滞后一两天；退回该主版本的最新构建，对驱动来说完全等价。
    fallback = _milestone_version(session, major)
    if fallback:
        found = attempt(fallback, _NPM_MIRROR, _MIRROR_SECONDS)
        if found:
            return found

    found = attempt(chrome_version, _CFT_BASE, _OFFICIAL_SECONDS)
    if found:
        return found
    if fallback:
        return attempt(fallback, _CFT_BASE, _OFFICIAL_SECONDS)
    return None


# --- 校验与落盘 --------------------------------------------------------------


def _extract_exe(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.endswith("chromedriver.exe")]
        if not names:
            raise ValueError("压缩包里没有 chromedriver.exe")
        return zf.read(names[0])


def _verify(raw: bytes, path: Path, major: int) -> str:
    """先看 PE 头，再真的跑一次 --version。

    跑不起来（比如当前进程没有执行权限）不算失败：记一条 WARNING 继续。
    跑起来但版本对不上才是失败 —— 那说明下到了错的东西。
    """
    if not raw.startswith(b"MZ"):
        raise ValueError("下载到的不是 Windows 可执行文件（没有 MZ 头）")

    try:
        proc = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("无法执行驱动自检（%s），按声明的版本继续", type(e).__name__)
        return ""

    text = f"{proc.stdout or ''}{proc.stderr or ''}"
    matched = re.search(r"ChromeDriver\s+(\d+\.\d+\.\d+\.\d+)", text)
    if not matched:
        raise ValueError(f"驱动自检读不出版本：{text.strip()[:120]!r}")
    got = matched.group(1)
    if browser_login._major_from_text(got) != major:
        raise ValueError(f"下载到的驱动是 {got}，和 Chrome 主版本 {major} 对不上")
    return got


def _install(raw: bytes, version: str, browser: str, major: int,
             platform_id: str) -> Path:
    """写进缓存目录。先写 .part 再原子改名，避免半截文件被当成可用驱动。

    目录布局必须和 browser_login._cached_driver_path 的 glob（`*/*/驱动名`）一致：
    <缓存根>/<chromedriver>/<平台>/<版本>/chromedriver.exe
    """
    folder, exe_name = browser_login._DRIVER_CACHE[browser]
    dest_dir = cache_root() / folder / platform_id / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / exe_name
    part = dest.with_name(exe_name + ".part")
    part.write_bytes(raw)
    try:
        _verify(raw, part, major)
    except ValueError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    return dest


# --- 冷却 -------------------------------------------------------------------


def _marker_path(cfg) -> Path:
    return Path(getattr(cfg, "base_dir", ".")) / _MARKER_NAME


def _read_marker(cfg) -> dict:
    try:
        data = json.loads(_marker_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(cfg, data: dict) -> None:
    try:
        _marker_path(cfg).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.debug("记录驱动更新状态失败（忽略）：%s", e)


def _cooldown_left(cfg, chrome_version: str) -> float:
    """上次对同一个 Chrome 版本失败后，还剩多少秒冷却。"""
    marker = _read_marker(cfg)
    if marker.get("chrome") != chrome_version:
        return 0.0
    failed_at = marker.get("failed_at")
    if not isinstance(failed_at, (int, float)):
        return 0.0
    return max(0.0, _COOLDOWN_HOURS * 3600 - (time.time() - failed_at))


def _fail(cfg, chrome_version: str, detail: str) -> UpdateResult:
    _write_marker(cfg, {"chrome": chrome_version, "failed_at": time.time()})
    return UpdateResult(
        False,
        f"{detail}（{_COOLDOWN_HOURS} 小时内不再重试）",
    )


# --- 并发锁 -----------------------------------------------------------------
#
# 计划任务每 5 分钟一轮，用户又可能同时手动跑 --update-driver。2026-11-24 实测
# 出现过两个进程各下一份 11 MB 的同一个包、而且都卡在慢速源上。
# 用一个 O_EXCL 创建的锁文件把后来者挡住即可；锁本身带年龄，崩溃留下的会自动接管。


def _lock_path(cfg) -> Path:
    return Path(getattr(cfg, "base_dir", ".")) / _LOCK_NAME


def _acquire_lock(cfg) -> bool:
    path = _lock_path(cfg)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                return False
            if age <= _LOCK_STALE_SECONDS:
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        except OSError:
            # 建不出锁（目录只读之类）就别拦着自己，宁可多下一次。
            return True
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def _release_lock(cfg) -> None:
    try:
        _lock_path(cfg).unlink(missing_ok=True)
    except OSError:
        pass


def update_if_needed(cfg, session=None, force: bool = False) -> UpdateResult:
    """需要更新时下载并安装驱动。

    force=True 忽略冷却（手动跑 --update-driver 时用）。
    任何失败都只返回 UpdateResult，不抛异常 —— 调用方是计划任务。
    """
    status = check(cfg)
    if not status.needs_update:
        return UpdateResult(True, status.detail)

    browser = getattr(cfg, "browser", "chrome")
    platform_id = _platform_id(browser)
    major = browser_login._major_from_text(status.chrome_version)
    if platform_id is None or major is None:
        return UpdateResult(False, status.detail)

    if not force:
        left = _cooldown_left(cfg, status.chrome_version)
        if left > 0:
            return UpdateResult(
                False,
                f"{status.detail}；上次更新失败，冷却中（还剩 {left / 3600:.1f} 小时）",
            )

    if not _acquire_lock(cfg):
        return UpdateResult(False, f"{status.detail}；另一个进程正在更新驱动，本轮跳过")

    try:
        session = session or requests.Session()
        found = _fetch_zip(session, status.chrome_version, major, platform_id)
        if not found:
            return _fail(cfg, status.chrome_version, "下载失败：所有源都不可用（没网或被挡）")

        version, data = found
        try:
            raw = _extract_exe(data)
            dest = _install(raw, version, browser, major, platform_id)
        except (ValueError, OSError, zipfile.BadZipFile) as e:
            return _fail(cfg, status.chrome_version, f"更新失败：{e}")

        _write_marker(cfg, {
            "chrome": status.chrome_version,
            "installed": version,
            "at": time.time(),
        })
        return UpdateResult(
            True,
            f"已更新：{status.detail}；现在用 {version}（{dest}）",
            changed=True,
        )
    finally:
        _release_lock(cfg)
