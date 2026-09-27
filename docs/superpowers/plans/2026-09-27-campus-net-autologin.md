# 校园网自动认证脚本 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让本机在断网或刚开机时，自动完成校园网锐捷 ePortal 网页认证，无需人工干预。

**Architecture:** 不做常驻进程，由 Windows 计划任务每 30 分钟（外加开机/解锁/网络切换事件）拉起 `login.py --check`。脚本先探测网络状态，仅在确认为"被 portal 劫持"时执行登录：拉登录页拿会话参数 → 取验证码 → ddddocr 本地识别 → 提交 → 解析结果。识别连续失败则弹 tkinter 窗口让人手填。密码用 Windows DPAPI 加密存储。全程有防锁号的失败计数与退避。

**Tech Stack:** Python 3.14、requests、ddddocr（onnxruntime 后端）、Pillow、tkinter、ctypes 调 DPAPI、pytest、Windows 计划任务

**设计文档：** `docs/superpowers/specs/2026-09-27-campus-net-autologin-design.md`

---

## 执行者必读

### 工作目录

所有命令都在 `C:\Users\De Ronaldinho\campus-net` 下执行。Git Bash 路径写作 `/c/Users/De Ronaldinho/campus-net`。

### 环境事实（已实测，不要重新试探）

- Python 3.14.3、pip 25.3
- 已装：`requests`、`Pillow` 12.2.0、`numpy` 2.4.4、`onnxruntime` 1.26.0、`opencv-python` 4.13.0.92
- **未装**：`ddddocr`、`pytest`（这两个是仅有的新增依赖）
- `tkinter` 可用
- 无 make、无 Linux、无容器。**不要**写依赖 POSIX 或 IPC 的代码
- MinGW 的 `pid_t` 是 `long long` 与本项目无关，忽略

### 两个外部输入，及其对计划的影响

1. **登录提交的确切端点与字段（抓包）** —— 尚未取得。
   本计划的对策：三个端点 URL（`login_page_url` / `login_submit_url` / `captcha_url`）**全部做成配置项**，默认值按锐捷 ePortal 的标准接口填写。取得抓包后若字段名不符，只需改 `config.json` 和 `portal._submit()` 里的字段名，不影响其他模块。**Task 12 专门做这件事。**
2. **验证码样本图片** —— 用于测 OCR 准确率，尚未提供。
   本计划的对策：Task 11 实现测量工具，但没有样本时先跳过，不影响 Task 1-10 和 13。准确率高低只决定"弹窗出现得频繁不频繁"，因为设计里本来就带弹窗兜底，**不会导致推倒重来**。

### 测试原则

- 全部单元测试**不碰真实校园网**。网络交互一律用 `tests/fakes.py` 里的 `FakeSession` 注入。
- 真实网络只在 Task 14 联调时用，且要控制次数（学校认证系统连续失败会锁号）。
- 禁止在测试里断言真实验证码的识别结果 —— 那是不确定行为。

### 编码约定

- 中文注释，简洁
- 日志用标准 `logging`，模块里 `log = logging.getLogger(__name__)`，不在模块内配置 handler（统一由 `logsetup.py` 配）
- 源码放 `src/`，测试放 `tests/`。所有模块用 `from src.xxx import yyy` 导入（`tests/conftest.py` 会处理 sys.path）

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `requirements.txt` | 依赖清单 |
| `pytest.ini` | pytest 配置（testpaths、日志） |
| `src/__init__.py` | 包标记 |
| `src/logsetup.py` | 日志初始化：格式、文件轮转 |
| `src/config.py` | `Config` 数据类 + `load()` + 校验 |
| `src/secret.py` | DPAPI 加解密（ctypes，无额外依赖） |
| `src/probe.py` | 四种网络状态判定 |
| `src/state.py` | 跨运行失败计数、防锁号状态 |
| `src/captcha.py` | 取验证码图片 + ddddocr 识别 |
| `src/portal.py` | 登录流程编排：`login()` + `_submit()` |
| `src/notify.py` | tkinter 弹窗 + 系统提示 |
| `src/accuracy.py` | 验证码样本准确率统计 |
| `login.py` | CLI 入口：`--check` / `--login` / `--setup` / `--test-captcha` |
| `install_task.ps1` | 注册/卸载计划任务 |
| `tests/fakes.py` | `FakeSession`、`FakeResponse` 测试替身 |
| `tests/test_*.py` | 各模块单元测试 |

---

## Task 1: 项目骨架与测试框架

**Files:**
- Create: `requirements.txt`
- Create: `pytest.ini`
- Create: `src/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`

- [ ] **Step 1: 建目录**

```bash
cd "/c/Users/De Ronaldinho/campus-net"
mkdir -p src tests
touch src/__init__.py tests/__init__.py
```

- [ ] **Step 2: 写 requirements.txt**

```
requests>=2.32
Pillow>=12.0
numpy>=2.0
opencv-python>=4.13
onnxruntime>=1.26
ddddocr>=1.6.1
pytest>=8.0
```

- [ ] **Step 3: 写 pytest.ini**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
log_cli = true
log_cli_level = INFO
```

- [ ] **Step 4: 写 .gitignore**

```
__pycache__/
*.pyc
.pytest_cache/
samples/
secret.dat
state.json
campus-net.log
campus-net.log.1
```

> `samples/` 不入库：里面是抓来的真实验证码图片，属于外部素材，且量会增长。

- [ ] **Step 5: 装依赖**

```bash
cd "/c/Users/De Ronaldinho/campus-net"
python -m pip install ddddocr pytest
```

Expected: 两个包下载安装成功。`onnxruntime`、`Pillow`、`numpy`、`opencv-python` 会显示 "Requirement already satisfied"。

- [ ] **Step 6: 验证**

```bash
python -c "import ddddocr; print('ddddocr ok')"
python -m pytest --version
```

Expected: 打印 `ddddocr ok` 和 pytest 版本号。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore: 项目骨架与依赖"
```

---

## Task 2: 测试替身 FakeSession

**Files:**
- Create: `tests/fakes.py`
- Create: `tests/conftest.py`

真实网络交互全部通过这两个文件注入，后续每个任务都要用。

- [ ] **Step 1: 写 tests/fakes.py**

```python
"""测试替身：模拟 requests.Session 的行为，避免单元测试碰真实网络。"""
from __future__ import annotations

import requests


class FakeResponse:
    def __init__(self, status_code=200, text="", content=b"", headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.content = content if content else text.encode("utf-8")
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    """按 URL（或 URL 前缀）匹配预置响应，避免单元测试碰真实网络。

    匹配只看 URL，与请求方法无关：为某个 URL 声明的条目，GET 和 POST 都会命中它。

    routes 可以是：
      - dict: {url: FakeResponse | Exception | list[...]}，按 url 精确匹配
      - list: [(url_prefix, FakeResponse | Exception | list[...]), ...]，
        按前缀匹配，先到先得（列表靠前的优先，**不是**最长前缀优先，
        所以更具体的前缀要写在前面）

    条目语义：
      - 非列表条目（FakeResponse 或 Exception 实例）：每次调用都返回/抛出，次数不限。
      - 列表条目：按**该路由自身**的调用顺序逐个消费，一次调用消费一个。
        队列耗尽后继续调用会抛 AssertionError —— 说明被测代码比测试声明的多调了一次。
        空列表 [] 在第一次调用时就抛 AssertionError。
        注意 [resp] 的含义是「只允许调用一次」，不是「一次之后一直重复」。

    匹配不到 URL 时同样抛 AssertionError，强迫测试显式声明所有请求。

    routes 不会被就地修改：消费状态放在实例私有的 _queues 里，
    因此同一个 routes 对象（fixture、模块级常量）可以安全地喂给多个 FakeSession。

    记录的调用：
      - calls: [(method, url), ...]，所有请求
      - posts: [(url, data), ...]，仅 POST，data 即提交的表单

    支持的 requests.Session 接口仅有：get、post、with 上下文管理器、headers。
    用到 .cookies 之类时，要扩展的是这个替身，而不是被测代码。
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.posts = []
        self.headers = {}
        self._queues = {}

    def _take(self, key, entry, method, url):
        """取出本次调用该用的条目。

        列表条目按调用顺序消费；队列耗尽（含空列表）时抛 AssertionError。
        队列是按 key（精确 url 或匹配到的前缀）惰性复制的，绝不改动调用方传入的 routes。
        """
        if not isinstance(entry, list):
            return entry
        queue = self._queues.setdefault(key, list(entry))
        if not queue:
            raise AssertionError(
                f"未预期的请求: {method} {url} —— "
                f"该路由预置的响应已用尽，被测代码比测试声明的多调用了一次"
            )
        return queue.pop(0)

    def _pick(self, method, url):
        self.calls.append((method, url))
        if isinstance(self.routes, dict):
            entry = self.routes.get(url)
            if entry is None:
                raise AssertionError(f"未预期的请求: {method} {url}")
            return self._take(url, entry, method, url)
        for prefix, entry in self.routes:
            if url.startswith(prefix):
                # 队列按「匹配到的前缀」而非完整 url 记录：
                # 前缀模式的 URL 每次都可能不同，但消费的是同一条队列。
                return self._take(prefix, entry, method, url)
        raise AssertionError(f"未预期的请求: {method} {url}")

    def _do(self, method, url, **kwargs):
        entry = self._pick(method, url)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def get(self, url, **kwargs):
        return self._do("GET", url, **kwargs)

    def post(self, url, data=None, **kwargs):
        # 先记账再匹配：即便这次请求未预期或抛了预置异常，也应留下记录
        self.posts.append((url, data))
        return self._do("POST", url, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def jsonp(obj: str) -> str:
    """把 JSON 串包成锐捷风格的 JSONP 响应体。"""
    return f"jQuery1124000000000000000_1700000000000({obj});"
```

- [ ] **Step 2: 写 tests/conftest.py**

```python
import logging
import sys
from pathlib import Path

import pytest

# 让 tests 里可以 `from src.xxx import yyy`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """每个用例跑完后，把 root logger 还原成本用例开始前的样子。

    logsetup.init() 把 handler 挂在 root logger 上，并把路径记在模块级的
    _configured 里，两者都活过整个 pytest 会话；而每个用例用的是新的 tmp_path。
    不还原的话 handler 会随用例数一路累积：日志重复写、旧临时文件被句柄占住删不掉，
    在 Windows 上就会变成难查的顺序相关失败。所以这段别当噪音删掉。
    """
    from src import logsetup

    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    configured_before = set(logsetup._configured)

    yield

    # 让用例之间互不影响：用过哪些日志路径也要还原
    logsetup._configured.clear()
    logsetup._configured.update(configured_before)

    # init() 会连 root 的级别一起改（Task 13 的 --verbose 就是传 DEBUG 进来的），
    # 级别不还原的话，后面的用例会一直按 DEBUG 往外写。
    root.setLevel(level_before)

    # 只清掉本用例新增的 handler，并 close 掉以释放文件句柄
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = handlers_before
```

- [ ] **Step 3: 写一个自检测试**

Create: `tests/test_fakes.py`

```python
import pytest
import requests
from tests.fakes import FakeSession, FakeResponse, jsonp


def test_fake_session_returns_response():
    s = FakeSession({"http://a/": FakeResponse(text="hi")})
    assert s.get("http://a/").text == "hi"
    assert s.calls == [("GET", "http://a/")]


def test_fake_session_raises_configured_exception():
    s = FakeSession({"http://a/": requests.Timeout("boom")})
    with pytest.raises(requests.Timeout):
        s.get("http://a/")


def test_fake_session_rejects_unexpected_url():
    s = FakeSession({})
    with pytest.raises(AssertionError):
        s.get("http://nope/")


def test_jsonp_wrapper():
    wrapped = jsonp('{"a":1}')
    assert wrapped.startswith("jQuery")
    assert wrapped.endswith('({"a":1});')


def test_non_list_entry_is_unlimited():
    """非列表条目可以无限次命中——目前所有计划的测试都靠这个行为。"""
    s = FakeSession({"http://a/": FakeResponse(text="hi")})
    assert [s.get("http://a/").text for _ in range(3)] == ["hi", "hi", "hi"]
    assert len(s.calls) == 3


def test_queue_consumed_in_declaration_order():
    s = FakeSession(
        {"http://a/": [FakeResponse(text="first"), FakeResponse(text="second")]}
    )
    assert s.get("http://a/").text == "first"
    assert s.get("http://a/").text == "second"


def test_queue_exhaustion_raises():
    """反封号保证：被测代码多调一次必须炸，而不是默默重复最后一个响应。"""
    s = FakeSession({"http://a/": [FakeResponse(text="only")]})
    assert s.post("http://a/", data={"x": 1}).text == "only"
    with pytest.raises(AssertionError, match="用尽"):
        s.post("http://a/", data={"x": 2})


def test_empty_queue_raises_on_first_call():
    s = FakeSession({"http://a/": []})
    with pytest.raises(AssertionError, match="用尽"):
        s.get("http://a/")


def test_routes_not_mutated_by_consumption():
    """同一个 routes 对象要能安全地喂给多个 FakeSession。"""
    routes = {"http://a/": [FakeResponse(text="first"), FakeResponse(text="second")]}

    s1 = FakeSession(routes)
    assert s1.get("http://a/").text == "first"
    assert s1.get("http://a/").text == "second"

    s2 = FakeSession(routes)
    assert s2.get("http://a/").text == "first"


def test_prefix_route_matches_longer_url():
    """前缀模式：测试声明的是前缀，实际 URL 带着没声明过的查询串也能命中。"""
    s = FakeSession([("http://a/api/", FakeResponse(text="ok"))])
    assert s.get("http://a/api/login?t=1700000000000&x=1").text == "ok"


def test_prefix_queue_keyed_by_prefix_not_full_url():
    """前缀模式下每次 URL 都不同，消费的必须是同一条队列。"""
    entry = [FakeResponse(text="first"), FakeResponse(text="second")]
    snapshot = list(entry)
    s = FakeSession([("http://a/api/", entry)])
    assert s.get("http://a/api/one").text == "first"
    assert s.get("http://a/api/two").text == "second"
    # 调用方传入的列表被完整消费过，但不该被改动
    assert entry == snapshot
    assert len(entry) == 2


def test_post_payload_recorded():
    """提交的表单字段是本项目风险最高的未知项（Task 12 专门要修正字段名），必须可断言。"""
    s = FakeSession({"http://a/": FakeResponse(text="ok")})
    s.post("http://a/", data={"username": "u", "password": "p"})
    assert s.posts == [("http://a/", {"username": "u", "password": "p"})]
    # calls 仍是二元组，计划的后续测试依赖这个形状
    assert s.calls == [("POST", "http://a/")]


def test_context_manager_does_not_swallow_exceptions():
    with FakeSession({"http://a/": FakeResponse(text="hi")}) as s:
        assert s.get("http://a/").text == "hi"
    with pytest.raises(AssertionError):
        with FakeSession({}) as s:
            s.get("http://nope/")


def test_headers_is_usable_dict():
    s = FakeSession({})
    s.headers["User-Agent"] = "x"
    assert s.headers == {"User-Agent": "x"}


def test_raise_for_status_at_boundaries():
    for code in (200, 399):
        FakeResponse(status_code=code).raise_for_status()
    for code in (400, 500):
        with pytest.raises(requests.HTTPError):
            FakeResponse(status_code=code).raise_for_status()
```

- [ ] **Step 4: 跑测试**

```bash
cd "/c/Users/De Ronaldinho/campus-net"
python -m pytest tests/test_fakes.py -v
```

Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: 网络交互测试替身 FakeSession"
```

---

## Task 3: logsetup.py — 日志初始化

**Files:**
- Create: `src/logsetup.py`
- Test: `tests/test_logsetup.py`

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_logsetup.py`

```python
import logging
import logging.handlers

import pytest

from src.logsetup import init


def test_init_creates_log_file(tmp_path):
    log_file = tmp_path / "x.log"
    init(str(log_file), "INFO")
    logging.getLogger("src.probe").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_file.exists()
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_init_does_not_duplicate_handlers(tmp_path):
    init(str(tmp_path / "a.log"), "INFO")
    n = len(logging.getLogger().handlers)
    init(str(tmp_path / "a.log"), "INFO")
    assert len(logging.getLogger().handlers) == n


def test_init_does_not_truncate_existing_log(tmp_path):
    log_file = tmp_path / "c.log"
    log_file.write_text("上一轮的记录\n", encoding="utf-8")
    init(str(log_file), "INFO")
    logging.getLogger("src.x").info("本轮")
    for h in logging.getLogger().handlers:
        h.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "上一轮的记录" in content and "本轮" in content


@pytest.fixture(scope="module")
def shared_log(tmp_path_factory):
    """下面两个用例共用同一个日志路径：第二个要验证“同一路径能再次 init”。"""
    return str(tmp_path_factory.mktemp("shared") / "shared.log")


def test_init_attaches_handlers(shared_log):
    before = len(logging.getLogger().handlers)
    init(shared_log, "INFO")
    assert len(logging.getLogger().handlers) == before + 2


def test_conftest_cleanup_is_not_cosmetic(shared_log):
    """依赖上一个用例：它用同一个路径调过 init()。

    1) 那个用例挂上去的 handler 必须已被 conftest 的 fixture 摘掉，否则会累积；
    2) _configured 里的路径也必须一并还原，否则这里的 init() 会被短路、一个
       handler 都不加。第 2 条是关键：只摘 handler 不还原路径，清理就是装样子的，
       而这条断言正是为了不让以后有人把那段 fixture 当噪音删掉。
    """
    leftovers = [h for h in logging.getLogger().handlers
                 if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert leftovers == []

    before = len(logging.getLogger().handlers)
    init(shared_log, "INFO")
    assert len(logging.getLogger().handlers) == before + 2


# 导入时（收集阶段，还没有用例跑过）先记下 root 的原始级别
_LEVEL_AT_IMPORT = logging.getLogger().level


def test_init_sets_root_level(tmp_path):
    init(str(tmp_path / "d.log"), "DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_root_level_is_restored():
    """级别也是会话级状态：init() 会改 root 级别，conftest 的 fixture 必须还原。

    上一个用例把级别设成了 DEBUG；fixture 若不还原，这里看到的就是 DEBUG，
    而不是导入时的原始级别。单独跑本用例时它是空转的——它是个泄漏探测器，
    靠的是同文件内的定义顺序（放最后），不是自身有多严格。
    """
    assert logging.getLogger().level == _LEVEL_AT_IMPORT
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_logsetup.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.logsetup'`

- [ ] **Step 3: 实现**

Create: `src/logsetup.py`

```python
"""统一的日志初始化。

所有模块自带 logger，但不在模块内配置 handler，一律由这里配置根 logger。
因为脚本是被计划任务反复拉起的短命进程，这里每次运行都会重新配置；
但如果同一个进程里被调用多次，不能重复添加 handler，否则日志会重复。
"""
from __future__ import annotations

import logging
import logging.handlers
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_configured: set[str] = set()


def init(log_path: str, level: str = "INFO") -> None:
    real_path = os.path.abspath(log_path)
    # 用 normcase 后的路径去重：Windows 上 a.log 和 A.LOG 是同一个文件，
    # 只用 abspath 会得到两个 key、两个 handler，日志写两遍。
    key = os.path.normcase(real_path)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if key in _configured:
        return

    os.makedirs(os.path.dirname(real_path) or ".", exist_ok=True)

    fmt = logging.Formatter(_FORMAT)

    fh = logging.handlers.RotatingFileHandler(
        real_path, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    _configured.add(key)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_logsetup.py -v
```

Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: 日志初始化，单文件轮转"
```

---

## Task 4: config.py — 配置加载与校验

**Files:**
- Create: `src/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_config.py`

```python
import json
import logging

import pytest

from src.config import DEFAULT_PROBE_URLS, Config, ConfigError, _KNOWN_KEYS, load


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
    }
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.config'`

- [ ] **Step 3: 实现**

Create: `src/config.py`

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_config.py -v
```

Expected: 52 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: 配置加载与校验，端点 URL 可配置"
```

---

## Task 5: secret.py — DPAPI 密码存储

**Files:**
- Create: `src/secret.py`
- Test: `tests/test_secret.py`

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_secret.py`

```python
import pytest

from src.secret import SecretError, load_password, save_password


def test_roundtrip(tmp_path):
    p = str(tmp_path / "secret.dat")
    save_password("hunter2", p)
    assert load_password(p) == "hunter2"


def test_roundtrip_non_ascii(tmp_path):
    p = str(tmp_path / "secret.dat")
    save_password("密码abc123!@#", p)
    assert load_password(p) == "密码abc123!@#"


def test_file_does_not_contain_plaintext(tmp_path):
    p = tmp_path / "secret.dat"
    save_password("hunter2", str(p))
    assert b"hunter2" not in p.read_bytes()


def test_missing_file_raises_readable_error(tmp_path):
    with pytest.raises(SecretError, match="找不到密码文件"):
        load_password(str(tmp_path / "nope.dat"))


def test_corrupt_file_raises_readable_error(tmp_path):
    p = tmp_path / "secret.dat"
    p.write_bytes(b"this is not a dpapi blob")
    with pytest.raises(SecretError, match="解密失败"):
        load_password(str(p))
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_secret.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.secret'`

- [ ] **Step 3: 实现**

Create: `src/secret.py`

```python
"""用 Windows DPAPI 加密保存密码。

不引入 keyring / pywin32，直接用 ctypes 调 crypt32。
加密结果绑定当前 Windows 账户：换账户或换机器都解不开，这是预期行为。
"""
from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

log = logging.getLogger(__name__)

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL

crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL

kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
kernel32.LocalFree.restype = wintypes.HLOCAL


class SecretError(Exception):
    """密码文件的读写或加解密失败。"""


def _make_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf  # 返回 buf 是为了保持引用，防止被 GC


def _protect(data: bytes) -> bytes:
    in_blob, _keep = _make_blob(data)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), "campus-net", None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise SecretError(f"加密失败，错误码 {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    in_blob, _keep = _make_blob(data)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise SecretError(
            f"解密失败，错误码 {ctypes.get_last_error()}。"
            "密码文件必须在加密它的同一个 Windows 账户下解密。"
        )
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_password(password: str, path: str) -> None:
    blob = _protect(password.encode("utf-8"))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)
    log.info("密码已加密保存到 %s", path)


def load_password(path: str) -> str:
    if not os.path.exists(path):
        raise SecretError(f"找不到密码文件: {path}。请先运行 python login.py --setup")
    with open(path, "rb") as f:
        blob = f.read()
    if not blob:
        raise SecretError(f"密码文件是空的: {path}。请重新运行 python login.py --setup")
    return _unprotect(blob).decode("utf-8")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_secret.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: DPAPI 加密存储密码"
```

---

## Task 6: probe.py — 四种网络状态判定

**Files:**
- Create: `src/probe.py`
- Test: `tests/test_probe.py`

判定规则（必须与设计文档 4.3 一致）：

1. 任一探测地址符合预期 → `ONLINE`
2. 否则看 portal 是否可达：不可达 → `PORTAL_DOWN`
3. portal 可达，且观察到重定向/HTML 劫持、或所有探测地址都没响应 → `UNAUTHED`
4. 其余情况（有响应但既不符合预期、也不像劫持，比如返回 500） → `UNKNOWN`

`PORTAL_DOWN` 与 `UNKNOWN` 都不触发登录。

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_probe.py`

```python
import requests

from src.config import Config
from src.probe import State, check
from tests.fakes import FakeResponse, FakeSession

PORTAL = "http://10.71.29.181"
PROBE1 = "http://www.msftconnecttest.com/connecttest.txt"
PROBE2 = "http://connect.rom.miui.com/generate_204"


def cfg(**over):
    base = dict(
        username="u", portal=PORTAL,
        probe_urls=[
            {"url": PROBE1, "expect_text": "Microsoft Connect Test"},
            {"url": PROBE2, "expect_status": 204},
        ],
        probe_timeout=1,
    )
    base.update(over)
    c = Config(**base)
    c.login_page_url = f"{PORTAL}/eportal/InterFace.do?method=pageInfo"
    c.login_submit_url = f"{PORTAL}/eportal/InterFace.do?method=login"
    c.captcha_url = f"{PORTAL}/eportal/validcode"
    return c


def test_online_when_probe_matches_text():
    s = FakeSession({PROBE1: FakeResponse(200, "Microsoft Connect Test\n")})
    assert check(cfg(), s) is State.ONLINE


def test_online_when_probe_matches_status():
    s = FakeSession({
        PROBE1: FakeResponse(302, "", headers={"Location": "http://portal/"}),
        PROBE2: FakeResponse(204, ""),
    })
    assert check(cfg(), s) is State.ONLINE


def test_unauthed_on_redirect_hijack():
    s = FakeSession({
        PROBE1: FakeResponse(302, "", headers={"Location": f"{PORTAL}/eportal/"}),
        PROBE2: FakeResponse(200, "<html>登录</html>", headers={"Content-Type": "text/html"}),
        PORTAL: FakeResponse(200, "<html>portal</html>"),
    })
    assert check(cfg(), s) is State.UNAUTHED


def test_unauthed_when_all_probes_timeout_but_portal_reachable():
    s = FakeSession({
        PROBE1: requests.Timeout("t"),
        PROBE2: requests.Timeout("t"),
        PORTAL: FakeResponse(200, "<html>portal</html>"),
    })
    assert check(cfg(), s) is State.UNAUTHED


def test_portal_down_when_everything_unreachable():
    s = FakeSession({
        PROBE1: requests.Timeout("t"),
        PROBE2: requests.ConnectionError("c"),
        PORTAL: requests.Timeout("t"),
    })
    assert check(cfg(), s) is State.PORTAL_DOWN


def test_portal_down_when_hijack_seen_but_portal_unreachable():
    s = FakeSession({
        PROBE1: FakeResponse(302, "", headers={"Location": "http://x/"}),
        PROBE2: requests.Timeout("t"),
        PORTAL: requests.Timeout("t"),
    })
    assert check(cfg(), s) is State.PORTAL_DOWN


def test_unknown_when_response_neither_expected_nor_hijack():
    s = FakeSession({
        PROBE1: FakeResponse(500, "server error", headers={"Content-Type": "text/plain"}),
        PROBE2: FakeResponse(500, "server error", headers={"Content-Type": "text/plain"}),
        PORTAL: FakeResponse(200, "<html>portal</html>"),
    })
    assert check(cfg(), s) is State.UNKNOWN


def test_probe_does_not_follow_redirects():
    """必须 allow_redirects=False，否则 302 被自动跟随，就看不出劫持了。"""
    seen = {}

    class S(FakeSession):
        def get(self, url, **kw):
            seen[url] = kw.get("allow_redirects")
            return super().get(url, **kw)

    s = S({PROBE1: FakeResponse(200, "Microsoft Connect Test")})
    check(cfg(), s)
    assert seen[PROBE1] is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_probe.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.probe'`

- [ ] **Step 3: 实现**

Create: `src/probe.py`

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_probe.py -v
```

Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: 四态网络探测，区分未认证与网络故障"
```

---

## Task 7: state.py — 防锁号计数

**Files:**
- Create: `src/state.py`
- Test: `tests/test_state.py`

规则（设计文档第 7 节）：

- 失败时间戳记入滚动窗口，窗口外的自动过期
- 窗口内失败次数达到 `window_max_fail` → 进入 `stopped`，停止自动登录
- 所有记录都过期后 → 自动解除 `stopped`（没有证据说明还有问题）
- 人工登录成功一次 → 清空全部计数

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_state.py`

```python
from src.state import State as FailState
from src.state import clear, is_stopped, load, prune, record_failure, save

HOUR = 3600


def test_load_missing_file_gives_empty_state(tmp_path):
    st = load(str(tmp_path / "nope.json"))
    assert st.failures == []
    assert st.stopped is False


def test_record_failure_appends_timestamp():
    st = FailState()
    record_failure(st, now=1000.0, window_hours=6, window_max_fail=3)
    assert st.failures == [1000.0]
    assert st.stopped is False


def test_stops_after_reaching_window_max_fail():
    st = FailState()
    for i in range(3):
        record_failure(st, now=1000.0 + i, window_hours=6, window_max_fail=3)
    assert st.stopped is True


def test_old_failures_expire_out_of_window():
    st = FailState()
    for i in range(3):
        record_failure(st, now=1000.0 + i, window_hours=6, window_max_fail=3)
    assert st.stopped is True
    # 6 小时后
    prune(st, now=1000.0 + 6 * HOUR + 1, window_hours=6)
    assert st.failures == []
    assert st.stopped is False


def test_partial_expiry_keeps_recent_failures():
    st = FailState()
    record_failure(st, now=0.0, window_hours=6, window_max_fail=99)
    record_failure(st, now=5 * HOUR, window_hours=6, window_max_fail=99)
    prune(st, now=6 * HOUR + 1, window_hours=6)
    assert st.failures == [5 * HOUR]


def test_clear_resets_everything():
    st = FailState()
    record_failure(st, now=0.0, window_hours=6, window_max_fail=1)
    assert st.stopped is True
    clear(st)
    assert st.failures == []
    assert st.stopped is False


def test_is_stopped_prunes_first():
    st = FailState()
    record_failure(st, now=0.0, window_hours=6, window_max_fail=1)
    assert is_stopped(st, now=1.0, window_hours=6) is True
    assert is_stopped(st, now=7 * HOUR, window_hours=6) is False


def test_save_and_load_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    st = FailState(failures=[1.0, 2.0], stopped=True)
    save(st, p)
    assert load(p) == st


def test_corrupt_state_file_is_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{oops", encoding="utf-8")
    assert load(str(p)) == FailState()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_state.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.state'`

- [ ] **Step 3: 实现**

Create: `src/state.py`

```python
"""跨运行的失败计数与防锁号状态。

学校认证系统连续失败会锁账号。宁可少试，也不要因为脚本死循环把账号试锁了——
锁号带来的麻烦远大于掉线。
所以失败计数跨运行累计，存 state.json。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)


@dataclass
class State:
    failures: list[float] = field(default_factory=list)
    stopped: bool = False


def load(path: str) -> State:
    if not os.path.exists(path):
        return State()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return State(
            failures=[float(x) for x in data.get("failures", [])],
            stopped=bool(data.get("stopped", False)),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        log.warning("状态文件损坏，按空状态处理: %s", e)
        return State()


def save(state: State, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f)
    os.replace(tmp, path)


def prune(state: State, now: float, window_hours: int) -> None:
    cutoff = now - window_hours * 3600
    state.failures = [t for t in state.failures if t >= cutoff]
    if state.stopped and not state.failures:
        log.info("失败记录已全部过期，自动解除停止状态")
        state.stopped = False


def record_failure(state: State, now: float, window_hours: int, window_max_fail: int) -> None:
    state.failures.append(now)
    prune(state, now, window_hours)
    if len(state.failures) >= window_max_fail:
        log.error(
            "最近 %d 小时内已有 %d 次失败，停止自动登录，需人工登录一次才能恢复",
            window_hours, len(state.failures),
        )
        state.stopped = True


def is_stopped(state: State, now: float, window_hours: int) -> bool:
    prune(state, now, window_hours)
    return state.stopped


def clear(state: State) -> None:
    state.failures = []
    state.stopped = False
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_state.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: 跨运行失败计数与防锁号状态"
```

---

## Task 8: captcha.py — 取验证码与识别

**Files:**
- Create: `src/captcha.py`
- Test: `tests/test_captcha.py`

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_captcha.py`

```python
import requests
import pytest

from src.captcha import fetch_image, recognize
from src.config import Config
from tests.fakes import FakeResponse, FakeSession

CAPTCHA_URL = "http://10.71.29.181/eportal/validcode"


def cfg():
    c = Config(username="u", portal="http://10.71.29.181", probe_timeout=1)
    c.captcha_url = CAPTCHA_URL
    return c


def test_fetch_image_returns_raw_bytes():
    s = FakeSession({CAPTCHA_URL: FakeResponse(200, content=b"\xff\xd8jpegbytes")})
    assert fetch_image(s, cfg()) == b"\xff\xd8jpegbytes"


def test_fetch_image_raises_on_http_error():
    s = FakeSession({CAPTCHA_URL: FakeResponse(404, "not found")})
    with pytest.raises(requests.HTTPError):
        fetch_image(s, cfg())


def test_fetch_image_passes_timeout():
    seen = {}

    class S(FakeSession):
        def get(self, url, **kw):
            seen.update(kw)
            return super().get(url, **kw)

    s = S({CAPTCHA_URL: FakeResponse(200, content=b"x")})
    fetch_image(s, cfg())
    assert seen["timeout"] == 1


def test_recognize_returns_str():
    """不测具体识别内容——那是不确定行为。只测接口契约。"""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (80, 30), "white").save(buf, format="PNG")
    result = recognize(buf.getvalue())
    assert isinstance(result, str)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_captcha.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.captcha'`

- [ ] **Step 3: 实现**

Create: `src/captcha.py`

```python
"""取验证码图片并用 ddddocr 本地识别。

关键：拉验证码必须和提交登录用同一个 Session，
验证码是与会话绑定的，换个 cookie 识别得再准也没用。
"""
from __future__ import annotations

import logging

from src.config import Config

log = logging.getLogger(__name__)

_ocr = None


def _get_ocr():
    """ddddocr 模型加载有开销，进程内只初始化一次。"""
    global _ocr
    if _ocr is None:
        import ddddocr
        log.debug("初始化 ddddocr 模型")
        _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


def fetch_image(session, cfg: Config) -> bytes:
    r = session.get(cfg.captcha_url, timeout=cfg.probe_timeout)
    r.raise_for_status()
    return r.content


def recognize(image_bytes: bytes) -> str:
    return _get_ocr().classification(image_bytes).strip()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_captcha.py -v
```

Expected: 4 passed（首次运行会加载模型，可能慢十几秒）

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: 验证码获取与 ddddocr 本地识别"
```

---

## Task 9: portal.py — 登录流程与提交

**Files:**
- Create: `src/portal.py`
- Test: `tests/test_portal.py`

**这是全项目唯一受"抓包未取得"影响的模块。**
`_submit()` 按锐捷 ePortal 标准接口实现。取得真实抓包后，Task 12 负责校对字段名。

> **测试替身用法（写测试前先看这段）**
>
> - 断言提交了什么字段，直接读 `s.posts`（形如 `[(url, data), ...]`），**不要**再写子类去捕获 —— 替身已经原生记录了。
> - 登录页地址将来若变成动态的（带 `flowSessionId` / `userIp` / `userMac`），路由改用**前缀形式** `[(url_prefix, response), ...]`，这样测试不必预先知道 query string。
> - **凡是要验证"调用了几次"的地方，路由必须声明成 list 队列**（如 `[fail_resp, fail_resp, ok_resp]`）并断言 `len(s.calls)` / `len(s.posts)`。非 list 的路由是无限次返回同一个响应，对调用次数**没有任何约束** —— 而被测代码多提交一次正是本项目最需要拦住的 bug。

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_portal.py`

```python
import pytest

from src.config import Config
from src.portal import Result, _parse_jsonp, login
from tests.fakes import FakeResponse, FakeSession, jsonp

PORTAL = "http://10.71.29.181"
PAGE_URL = f"{PORTAL}/eportal/InterFace.do?method=pageInfo"
SUBMIT_URL = f"{PORTAL}/eportal/InterFace.do?method=login"
CAPTCHA_URL = f"{PORTAL}/eportal/validcode"
QS = "wlanuserip%3D10.40.7.250%26wlanacname%3Dac1"


def cfg(tmp_path, **over):
    c = Config(username="2021001", portal=PORTAL, probe_timeout=1, **over)
    c.login_page_url = PAGE_URL
    c.login_submit_url = SUBMIT_URL
    c.captcha_url = CAPTCHA_URL
    c.secret_path = str(tmp_path / "secret.dat")
    return c


@pytest.fixture
def secret_file(tmp_path):
    from src.secret import save_password
    p = str(tmp_path / "secret.dat")
    save_password("pw123", p)
    return p


def page_ok():
    return FakeResponse(200, jsonp('{"result":"success","queryString":"' + QS + '"}'))


def test_parse_jsonp_extracts_object():
    assert _parse_jsonp(jsonp('{"a":1}')) == {"a": 1}


def test_parse_jsonp_rejects_non_jsonp():
    with pytest.raises(ValueError):
        _parse_jsonp("<html>nope</html>")


def test_login_success(tmp_path, secret_file):
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, jsonp('{"result":"success","message":"ok"}')),
    })
    res = login(cfg(tmp_path), s, captcha_text="abcd")
    assert res.ok is True
    assert res.reason == "success"


def test_login_sends_expected_form_fields(tmp_path, secret_file):
    """断言提交表单的字段，同时钉死"只提交一次"。

    只提交一次是这个项目的核心安全属性：学校的认证系统连续失败会锁账号，
    而每一次多余的提交都在消耗重试额度。所以这里不顺带断言就不算测到位。
    """
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, jsonp('{"result":"success"}')),
    })
    login(cfg(tmp_path), s, captcha_text="abcd")

    assert len(s.posts) == 1, "一次 login() 只能提交一次登录表单"
    url, d = s.posts[0]
    assert url == SUBMIT_URL
    assert d["userId"] == "2021001"
    assert d["password"] == "pw123"
    assert d["validcode"] == "abcd"
    assert d["queryString"] == QS
    assert d["passwordEncrypt"] == "false"


def test_login_uses_same_session_for_captcha_and_submit(tmp_path, secret_file):
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, jsonp('{"result":"success"}')),
    })
    login(cfg(tmp_path), s, captcha_text="abcd")
    assert [c[0] for c in s.calls] == ["GET", "GET", "POST"]
    assert s.calls[1][1] == CAPTCHA_URL
    assert s.calls[2][1] == SUBMIT_URL


def test_login_classifies_bad_captcha(tmp_path, secret_file):
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, jsonp('{"result":"fail","message":"验证码错误"}')),
    })
    res = login(cfg(tmp_path), s, captcha_text="abcd")
    assert res.ok is False
    assert res.reason == "bad_captcha"
    assert res.captcha_image == b"img"


def test_login_classifies_bad_credentials(tmp_path, secret_file):
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, jsonp('{"result":"fail","message":"用户名或密码错误"}')),
    })
    res = login(cfg(tmp_path), s, captcha_text="abcd")
    assert res.reason == "bad_credentials"


def test_login_classifies_bad_credentials_by_password_word(tmp_path, secret_file):
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, jsonp('{"result":"fail","message":"密码不正确"}')),
    })
    assert login(cfg(tmp_path), s, captcha_text="abcd").reason == "bad_credentials"


def test_login_classifies_network_error(tmp_path, secret_file):
    import requests
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: requests.Timeout("t"),
    })
    assert login(cfg(tmp_path), s, captcha_text="abcd").reason == "network"


def test_login_without_captcha_text_uses_ocr(tmp_path, secret_file, monkeypatch):
    """captcha_text=None 时应走 OCR。这里把 OCR 打桩，避免依赖真实识别结果。"""
    monkeypatch.setattr("src.portal.captcha.recognize", lambda b: "zzzz")
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, jsonp('{"result":"success"}')),
    })
    assert login(cfg(tmp_path), s).ok is True


def test_login_empty_ocr_result_is_bad_captcha(tmp_path, secret_file, monkeypatch):
    monkeypatch.setattr("src.portal.captcha.recognize", lambda b: "")
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
    })
    res = login(cfg(tmp_path), s)
    assert res.reason == "bad_captcha"


def test_login_handles_non_jsonp_submit_response(tmp_path, secret_file):
    """提交返回 HTML（某些 portal 会这样）时不能崩，应判为失败。"""
    s = FakeSession({
        PAGE_URL: page_ok(),
        CAPTCHA_URL: FakeResponse(200, content=b"img"),
        SUBMIT_URL: FakeResponse(200, "<html>error</html>"),
    })
    res = login(cfg(tmp_path), s, captcha_text="abcd")
    assert res.ok is False
    assert res.reason == "unknown"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_portal.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.portal'`

- [ ] **Step 3: 实现**

Create: `src/portal.py`

```python
"""登录流程编排。

对外只暴露 login()，调用方不关心中间步骤。
每次调用只做一次完整尝试，重试编排在 login.py，不在这里。

_submit() 是唯一受"抓包未取得"影响的函数：
按锐捷 ePortal 标准接口实现，取得真实抓包后校对字段名即可。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from src import captcha, secret
from src.config import Config

log = logging.getLogger(__name__)

# 提交失败时，从服务端消息里判定失败原因的关键词。
# 顺序重要：先判验证码，再判账号密码。
_BAD_CAPTCHA_WORDS = ("验证码", "校验码", "captcha", "validcode")
_BAD_CREDENTIAL_WORDS = ("密码", "用户名", "账号", "用户不存在", "password", "userid")


@dataclass
class Result:
    ok: bool
    reason: str          # success | bad_captcha | bad_credentials | network | unknown
    detail: str = ""
    captcha_image: bytes | None = None


def _parse_jsonp(text: str) -> dict:
    """解析锐捷风格的 JSONP：jQuery123(...);"""
    t = text.strip().rstrip(";").strip()
    i, j = t.find("("), t.rfind(")")
    if i == -1 or j == -1 or j < i:
        raise ValueError(f"不是 JSONP 响应: {text[:120]!r}")
    return json.loads(t[i + 1:j])


def _classify(message: str) -> str:
    low = message.lower()
    if any(w in message or w in low for w in _BAD_CAPTCHA_WORDS):
        return "bad_captcha"
    if any(w in message or w in low for w in _BAD_CREDENTIAL_WORDS):
        return "bad_credentials"
    return "unknown"


def _submit(session, cfg: Config, payload: dict) -> tuple[bool, str]:
    """提交登录表单。返回 (是否成功, 服务端消息)。

    取得真实抓包后，若字段名与此处不符，改这里和 config.json 的 URL 即可。
    """
    try:
        r = session.post(cfg.login_submit_url, data=payload, timeout=cfg.probe_timeout)
    except requests.RequestException as e:
        return False, f"NETWORK:{e}"

    try:
        data = _parse_jsonp(r.text)
    except (ValueError, json.JSONDecodeError):
        log.warning("提交响应不是 JSONP，原文前 200 字: %s", r.text[:200])
        return False, r.text[:200]

    result = str(data.get("result", "")).lower()
    message = str(data.get("message", ""))
    return result == "success", message


def _build_payload(cfg: Config, query_string: str, validcode: str) -> dict:
    return {
        "userId": cfg.username,
        "password": secret.load_password(cfg.secret_path),
        "service": "",
        "queryString": query_string,
        "operatorPwd": "",
        "operatorUserId": "",
        "validcode": validcode,
        "passwordEncrypt": "false",
    }


def login(cfg: Config, session, captcha_text: str | None = None) -> Result:
    # 1. 拉登录页，拿到本次流程的 queryString
    try:
        r = session.get(cfg.login_page_url, timeout=cfg.probe_timeout)
    except requests.RequestException as e:
        return Result(False, "network", f"拉取登录页失败: {e}")

    try:
        query_string = _parse_jsonp(r.text).get("queryString", "")
    except (ValueError, json.JSONDecodeError) as e:
        return Result(False, "unknown", f"登录页响应无法解析: {e}")

    # 2. 取验证码（同一个 session）
    try:
        image = captcha.fetch_image(session, cfg)
    except requests.RequestException as e:
        return Result(False, "network", f"拉取验证码失败: {e}")

    # 3. 识别
    code = captcha_text if captcha_text is not None else captcha.recognize(image)
    if not code:
        return Result(False, "bad_captcha", "识别结果为空", image)

    # 4. 提交
    try:
        payload = _build_payload(cfg, query_string, code)
    except secret.SecretError as e:
        return Result(False, "unknown", str(e), image)

    ok, message = _submit(session, cfg, payload)
    if ok:
        log.info("登录成功")
        return Result(True, "success", message, image)

    if message.startswith("NETWORK:"):
        return Result(False, "network", message[len("NETWORK:"):], image)

    reason = _classify(message)
    log.warning("登录失败 (%s): %s", reason, message)
    return Result(False, reason, message, image)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_portal.py -v
```

Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: 登录流程与提交，端点字段可校对"
```

---

## Task 10: notify.py — 弹窗兜底

**Files:**
- Create: `src/notify.py`
- Test: `tests/test_notify.py`

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_notify.py`

```python
from src import notify as notify_mod
from src.notify import _looks_interactive, notify


def test_looks_interactive_returns_bool():
    assert isinstance(_looks_interactive(), bool)


def test_notify_degrades_to_log_without_display(monkeypatch, caplog):
    """无桌面会话时不能崩，只能降级为日志。

    必须把 _looks_interactive 打桩成 False——否则真弹出一个模态窗口，
    测试会一直卡在那里等人点"知道了"。
    """
    monkeypatch.setattr(notify_mod, "_looks_interactive", lambda: False)
    with caplog.at_level("WARNING"):
        notify("标题", "内容")
    assert "内容" in caplog.text
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_notify.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.notify'`

- [ ] **Step 3: 实现**

Create: `src/notify.py`

```python
"""弹窗兜底与提示。

OCR 连续失败后，弹一个小窗让人手填验证码。
用 Python 自带的 tkinter，不引入额外依赖。

脚本由计划任务以"仅在用户登录时运行"启动，所以正常情况下弹得出来；
但万一没有可交互桌面会话（比如被别的方式拉起），
不能崩，只能降级为写日志。
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)


def _looks_interactive() -> bool:
    """粗略判断当前有没有可用的桌面会话。"""
    try:
        import tkinter
    except ImportError:
        return False
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


def notify(title: str, message: str) -> None:
    log.warning("%s | %s", title, message)
    if not _looks_interactive():
        return
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        tk.Label(root, text=message, justify="left", wraplength=420).pack(padx=20, pady=16)
        tk.Button(root, text="知道了", command=root.destroy).pack(pady=(0, 16))
        root.mainloop()
    except Exception as e:
        log.debug("弹窗失败，已降级为日志: %s", e)


def ask_captcha(image_bytes: bytes, cfg) -> str | None:
    """显示验证码图片，让用户手填。关闭窗口返回 None。"""
    if not _looks_interactive():
        log.error("没有可交互桌面会话，无法弹窗")
        return None

    import tkinter as tk
    from PIL import Image, ImageTk

    try:
        img = Image.open(io.BytesIO(image_bytes))
        # 验证码图通常很小，放大 3 倍便于辨认
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    except Exception as e:
        log.error("验证码图片无法打开: %s", e)
        return None

    root = tk.Tk()
    root.title("校园网认证 — 请输入验证码")
    root.attributes("-topmost", True)

    photo = ImageTk.PhotoImage(img)
    tk.Label(root, image=photo).pack(padx=20, pady=(16, 8))
    tk.Label(root, text=f"账号：{cfg.username}").pack()
    tk.Label(root, text="自动识别失败，请手动输入验证码后回车",
             fg="#666", font=("Microsoft YaHei", 9)).pack(pady=(2, 8))

    var = tk.StringVar()
    entry = tk.Entry(root, textvariable=var, font=("Consolas", 20),
                     width=8, justify="center")
    entry.pack(pady=4)
    entry.focus_set()

    result: dict[str, str | None] = {"value": None}

    def submit(event=None):
        result["value"] = var.get().strip()
        root.destroy()

    entry.bind("<Return>", submit)
    tk.Button(root, text="提交", command=submit, width=12).pack(pady=(8, 16))
    root.mainloop()

    value = result["value"]
    if not value:
        log.info("用户取消了验证码输入")
        return None
    return value
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_notify.py -v
```

Expected: 2 passed（都走日志降级路径，不会弹出任何窗口，不会卡住）

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: tkinter 弹窗兜底与提示"
```

---

## Task 11: accuracy.py + CLI 的 --test-captcha

**Files:**
- Create: `src/accuracy.py`
- Test: `tests/test_accuracy.py`

**样本命名约定**：文件名即正确答案，形如 `3f7k.png`；同名加序号时写作 `3f7k_1.png`（只取第一个 `_` 之前的部分作为标签）。标签不是纯字母数字时视为无标签，只打印不统计。

这样测准确率不需要人工逐张标注——用户存图时本来就在看那几个字符，顺手命名即可。

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_accuracy.py`

```python
from src.accuracy import Results, label_of, measure, report


def test_label_from_plain_filename():
    assert label_of("3f7k.png") == "3f7k"


def test_label_strips_trailing_ordinal():
    assert label_of("3f7k_1.png") == "3f7k"
    assert label_of("3f7k_12.png") == "3f7k"


def test_label_empty_when_not_alnum():
    assert label_of("截图 2026-09-27.png") is None
    assert label_of("____.png") is None


def test_label_uppercase_is_lowered():
    assert label_of("3F7K.png") == "3f7k"


def test_measure_reports_accuracy_and_misses(tmp_path):
    from pathlib import Path
    for name in ("abcd.png", "efgh.png", "ijkl.png", "mnop.png"):
        (tmp_path / name).write_bytes(b"x")

    answers = {"abcd": "abcd", "efgh": "efgh", "ijkl": "ijkl", "mnop": "XXXX"}

    def fake_recognize(path: Path, data: bytes) -> str:
        return answers[path.stem]

    results = measure(str(tmp_path), recognize_fn=fake_recognize)
    assert results.total == 4
    assert results.correct == 3
    assert results.accuracy == 0.75
    assert results.misses == [("mnop", "XXXX")]


def test_measure_skips_unlabelled_files(tmp_path):
    (tmp_path / "abcd.png").write_bytes(b"x")
    (tmp_path / "random screenshot.png").write_bytes(b"x")
    results = measure(str(tmp_path), recognize_fn=lambda p, b: "abcd")
    assert results.total == 1
    assert results.correct == 1


def test_measure_missing_dir_returns_empty(tmp_path):
    results = measure(str(tmp_path / "nope"), recognize_fn=lambda p, b: "x")
    assert results.total == 0


def test_report_handles_zero_samples():
    text = report(Results(total=0, correct=0, misses=[]))
    assert "没有" in text


def test_report_includes_accuracy_and_misses():
    text = report(Results(total=10, correct=9, misses=[("abcd", "abce")]))
    assert "90.0%" in text
    assert "abcd" in text
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_accuracy.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.accuracy'`

- [ ] **Step 3: 实现**

Create: `src/accuracy.py`

```python
"""验证码识别准确率统计。

样本文件名即正确答案（如 3f7k.png），所以不需要人工逐张标注。
用途：先导测试，决定 OCR 主力这条路走不走得通。

判定标准：>=80% 按设计走；60~80% 提高重试次数；<60% 应改为以弹窗为主。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


@dataclass
class Results:
    total: int
    correct: int
    misses: list[tuple[str, str]] = field(default_factory=list)
    unlabelled: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def label_of(filename: str) -> str | None:
    """从文件名推出正确答案。取第一个下划线前的部分，必须是纯字母数字。"""
    stem = Path(filename).stem
    label = stem.split("_")[0].strip().lower()
    if not label or not label.isalnum():
        return None
    return label


def measure(samples_dir: str, recognize_fn=None) -> Results:
    if recognize_fn is None:
        from src.captcha import recognize as _recognize

        def recognize_fn(path: Path, data: bytes) -> str:
            return _recognize(data)

    if not os.path.isdir(samples_dir):
        log.warning("样本目录不存在: %s", samples_dir)
        return Results(total=0, correct=0)

    total = correct = unlabelled = 0
    misses: list[tuple[str, str]] = []

    for name in sorted(os.listdir(samples_dir)):
        path = Path(samples_dir) / name
        if not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        label = label_of(name)
        if label is None:
            unlabelled += 1
            continue
        data = path.read_bytes()
        try:
            got = recognize_fn(path, data).strip().lower()
        except Exception as e:
            log.warning("识别 %s 出错: %s", name, e)
            got = ""
        total += 1
        if got == label:
            correct += 1
        else:
            misses.append((label, got))

    return Results(total=total, correct=correct, misses=misses, unlabelled=unlabelled)


def report(results: Results) -> str:
    lines = []
    if results.total == 0:
        lines.append("没有可用的验证码样本。")
        lines.append("")
        lines.append("请把登录页上的验证码图片右键另存为到 samples/ 目录，")
        lines.append("文件名写成图片里的字符，例如 3f7k.png。这样就能自动统计准确率。")
        if results.unlabelled:
            lines.append(f"（另有 {results.unlabelled} 张文件名不是纯字母数字，已跳过）")
        return "\n".join(lines)

    lines.append(f"样本数: {results.total}")
    lines.append(f"识别正确: {results.correct}")
    lines.append(f"准确率: {results.accuracy:.1%}")
    if results.unlabelled:
        lines.append(f"跳过（文件名无标签）: {results.unlabelled}")

    if results.misses:
        lines.append("")
        lines.append("识别错误的样本（正确 → 识别结果）:")
        for label, got in results.misses[:20]:
            lines.append(f"  {label} → {got or '(空)'}")

    lines.append("")
    acc = results.accuracy
    if acc >= 0.80:
        lines.append("判定：准确率达标，按设计走 OCR 主力 + 弹窗兜底。")
    elif acc >= 0.60:
        lines.append("判定：尚可。建议把 captcha_max_retry 提到 8，并接受偶发弹窗。")
    else:
        lines.append("判定：偏低。建议改为以弹窗为主，OCR 只做尝试性预填。")

    return "\n".join(lines)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_accuracy.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: 验证码识别准确率统计工具"
```

---

## Task 12: 用真实抓包校对提交接口

**Files:**
- Modify: `config.json`（Task 14 创建）
- Modify: `src/portal.py`（`_submit` 与 `_build_payload`）
- Modify: `tests/test_portal.py`（字段名变更时同步）

**前置：必须先取得点登录时的抓包。** 取得之前不要做本任务，其余任务不受影响。

> **⚠️ 先判断一件事：登录页地址是静态的还是动态的？**
>
> 设计文档第 2 节记的那条登录页 URL 带了三个**运行时才有的**参数：
> `flowSessionId`（每次流程现生成）、`userIp`、`userMac`（本机 IP 和 MAC）。
> 而目前 `config.json` 里 `login_page_url` 只是一个**静态字符串**——它表达不了这种动态 URL。
>
> 两种可能，对应两种做法：
>
> - **静态**（大概率）：锐捷标准的 `InterFace.do?method=pageInfo` 由 AC 按源 IP 识别客户端，脚本直接请求固定地址即可。那就按本任务原样做，什么都不用加。
> - **动态**：如果抓包显示是 AC 重定向过来的那条带参 URL，那么静态字符串这条路走不通，需要改成**让脚本自己跟随一次未认证请求的重定向**去发现登录页地址（请求探测地址 → 不开 `allow_redirects` → 读 `Location` 头 → 用那个 URL）。这才是通用做法，且不依赖把 IP/MAC 拼进模板。
>
> **这条必须在动手改字段之前定下来**，否则 Task 12 "改 config.json 就行" 的承诺对这个字段是不成立的。若判定为动态，先回来改设计与计划，再继续。

- [ ] **Step 1: 提取抓包里的关键信息**

从 cURL 里读出三样东西：

1. 提交的**完整 URL**（含 query string，如 `?method=login`）
2. 提交的**表单字段名**（`--data-raw` / `-d` 后面的内容，密码那个值已脱敏）
3. 响应的**判定方式**（`result` 字段名？还是 HTTP 状态码？还是跳转？）

- [ ] **Step 2: 与当前实现逐项比对**

| 比对项 | 当前默认值 | 抓包实际值 | 是否要改 |
|---|---|---|---|
| 提交 URL | `{portal}/eportal/InterFace.do?method=login` | | |
| 账号字段 | `userId` | | |
| 密码字段 | `password` | | |
| 验证码字段 | `validcode` | | |
| 会话参数字段 | `queryString` | | |
| 密码是否加密 | `passwordEncrypt=false` | | |
| 成功判定 | `result == "success"` | | |

- [ ] **Step 3: 按比对结果改 config.json**

URL 不符就改 URL，不用动代码：

```json
{
  "login_submit_url": "抓包里的真实提交地址",
  "login_page_url": "抓包里拉取登录页的地址",
  "captcha_url": "抓包里取验证码图片的地址"
}
```

- [ ] **Step 4: 字段名不符则改 _build_payload**

`src/portal.py` 的 `_build_payload`，把 key 换成抓包里的真实字段名。例如若实际是 `userName` / `pwd` / `code`：

```python
def _build_payload(cfg: Config, query_string: str, validcode: str) -> dict:
    return {
        "userName": cfg.username,
        "pwd": secret.load_password(cfg.secret_path),
        "service": "",
        "queryString": query_string,
        "operatorPwd": "",
        "operatorUserId": "",
        "code": validcode,
        "passwordEncrypt": "false",
    }
```

同步更新 `tests/test_portal.py::test_login_sends_expected_form_fields` 里的断言 key。

- [ ] **Step 5: 成功判定不符则改 _submit**

若响应不是 `{"result":"success"}` 而是别的形状，改 `_submit` 末尾：

```python
    result = str(data.get("result", "")).lower()
    message = str(data.get("message", ""))
    return result == "success", message
```

（若判定依据是别的字段或 HTTP 状态码，在这里替换成对应逻辑。）

- [ ] **Step 6: 若密码是前端加密的**

抓包里若看到密文（一长串 base64/hex）而不是明文，说明前端做了加密。此时：

1. 在登录页源码里找到加密函数（常见名 `strEnc`、`encrypt`、`doEncrypt`）
2. 在 `src/portal.py` 新增 `src/encrypt.py` 用 Python 复刻该算法
3. `_build_payload` 里改为 `"password": encrypt(secret.load_password(cfg.secret_path), ...)`，并把 `passwordEncrypt` 改成抓包里的值
4. 为 `src/encrypt.py` 补一个单元测试：把抓包里的明文/密文对作为测试向量，断言输出一致

**这是本计划中唯一可能显著超出预估的情况**，发生时先停下来评估工作量，再决定继续还是退回方案 B。

- [ ] **Step 7: 跑全部单元测试**

```bash
python -m pytest -v
```

Expected: 全部通过

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "fix: 按真实抓包校对提交接口字段"
```

---

## Task 13: login.py — CLI 编排

**Files:**
- Create: `login.py`
- Test: `tests/test_cli.py`

> **本任务的重点是重试与防锁号，测试必须钉死"试了几次"。**
> 这里最容易写出的坏测试是：把 `portal.login` 打桩成永远失败、断言最终返回 1 —— 那样
> 无论代码重试 3 次还是 30 次都会通过，而"重试太多次把账号试锁"正是本任务存在的理由。
> 所以：
>
> - 每次调用要用可计数的桩（如 `monkeypatch` 一个记录调用次数的函数），断言**精确次数**。
> - 验证"验证码错 → 换一张重试"时，用 list 队列路由 `[bad, bad, ok]` 并断言
>   `len(s.calls)`；用非 list 路由等于放弃对次数的约束。
> - `bad_credentials` 那条路径要单独断言**调用次数为 1**（确定性失败不做任何重试）。

- [ ] **Step 1: 写失败的测试**

Create: `tests/test_cli.py`

```python
"""CLI 编排测试。全部打桩，不碰真实网络。

注意：这些测试都不打桩 requests.Session 本身——创建一个 Session 对象不会发起
网络请求，而所有会发包的函数（probe.check、do_login）都被打桩了。
"""
import json

import pytest

import login as cli
from src import probe


def make_cfg(tmp_path, **over):
    data = {"username": "2021001", "portal": "http://10.71.29.181"}
    data.update(over)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_check_exits_zero_when_online(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.probe, "check", lambda cfg, s: probe.State.ONLINE)
    assert cli.main(["--check", "--config", make_cfg(tmp_path)]) == 0


def test_check_exits_zero_and_skips_login_when_portal_down(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cli.probe, "check", lambda cfg, s: probe.State.PORTAL_DOWN)
    monkeypatch.setattr(cli, "do_login", lambda cfg, s, st, now: called.append(1) or 0)
    assert cli.main(["--check", "--config", make_cfg(tmp_path)]) == 0
    assert called == []


def test_check_exits_zero_and_skips_login_when_unknown(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cli.probe, "check", lambda cfg, s: probe.State.UNKNOWN)
    monkeypatch.setattr(cli, "do_login", lambda cfg, s, st, now: called.append(1) or 0)
    assert cli.main(["--check", "--config", make_cfg(tmp_path)]) == 0
    assert called == []


def test_check_logs_in_when_unauthed(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cli.probe, "check", lambda cfg, s: probe.State.UNAUTHED)
    monkeypatch.setattr(cli, "do_login", lambda cfg, s, st, now: called.append(1) or 0)
    assert cli.main(["--check", "--config", make_cfg(tmp_path)]) == 0
    assert called == [1]


def test_check_respects_stopped_state(monkeypatch, tmp_path):
    path = make_cfg(tmp_path)
    st_file = tmp_path / "state.json"
    st_file.write_text(json.dumps({"failures": [1e12], "stopped": True}), encoding="utf-8")

    called = []
    monkeypatch.setattr(cli.probe, "check", lambda cfg, s: probe.State.UNAUTHED)
    monkeypatch.setattr(cli, "do_login", lambda cfg, s, st, now: called.append(1) or 0)
    monkeypatch.setattr(cli.time, "time", lambda: 1e12 + 60)
    monkeypatch.setattr(cli.notify, "notify", lambda *a, **k: None)

    assert cli.main(["--check", "--config", path]) == 1
    assert called == []


def test_main_exits_two_on_config_error(tmp_path):
    assert cli.main(["--check", "--config", str(tmp_path / "nope.json")]) == 2


def test_bad_argument_exits_two(tmp_path):
    # argparse 遇到未知参数是直接 sys.exit(2)，不是 return 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--nonsense", "--config", make_cfg(tmp_path)])
    assert exc.value.code == 2


def test_test_captcha_prints_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli.accuracy, "measure",
        lambda d, recognize_fn=None: cli.accuracy.Results(total=2, correct=2),
    )
    assert cli.main(["--test-captcha", "--config", make_cfg(tmp_path)]) == 0
    assert "100.0%" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_cli.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'login'`

- [ ] **Step 3: 实现**

Create: `login.py`

```python
#!/usr/bin/env python
"""校园网自动认证 CLI。

用法：
  python login.py --check          供计划任务调用，探测并按需登录
  python login.py --login          强制登录一次（跳过探测），调试用
  python login.py --setup          交互式保存账号密码
  python login.py --test-captcha   统计 samples/ 里验证码的识别准确率

退出码：0 已在线或登录成功；1 登录失败；2 配置或环境错误。
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import time

import requests

from src import accuracy, logsetup, notify, portal, probe, secret, state
from src.config import ConfigError, load

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    return s


def do_login(cfg, session, st, now: float) -> int:
    """执行一轮登录：OCR 重试若干次，失败则弹窗兜底。"""
    run_fails = 0
    last_image = None

    for attempt in range(1, cfg.captcha_max_retry + 1):
        res = portal.login(cfg, session)
        if res.ok:
            log.info("登录成功（第 %d 次尝试）", attempt)
            state.clear(st)
            state.save(st, cfg.state_path)
            return 0

        last_image = res.captcha_image
        log.warning("登录失败 (%s): %s", res.reason, res.detail)

        if res.reason == "bad_credentials":
            log.error("账号或密码错误，停止重试")
            notify.notify("校园网认证", f"账号或密码错误，已停止自动重试。\n{res.detail}")
            state.record_failure(st, now, cfg.window_hours, cfg.window_max_fail)
            state.save(st, cfg.state_path)
            return 1

        if res.reason == "network":
            log.error("网络异常，本轮放弃")
            break

        run_fails += 1
        if run_fails >= cfg.run_max_fail:
            log.warning("本次运行已失败 %d 次，停止自动重试", run_fails)
            break

    # OCR 重试用尽 → 弹窗兜底
    if last_image:
        log.info("转入弹窗模式，等待人工输入验证码")
        text = notify.ask_captcha(last_image, cfg)
        if text:
            res = portal.login(cfg, session, captcha_text=text)
            if res.ok:
                log.info("弹窗提交成功")
                state.clear(st)
                state.save(st, cfg.state_path)
                return 0
            log.error("弹窗提交仍失败 (%s): %s", res.reason, res.detail)

    state.record_failure(st, now, cfg.window_hours, cfg.window_max_fail)
    state.save(st, cfg.state_path)
    return 1


def _status(st: probe.State) -> str:
    return st.value


def run_check(cfg) -> int:
    now = time.time()
    st = state.load(cfg.state_path)
    state.prune(st, now, cfg.window_hours)

    with _new_session() as session:
        net = probe.check(cfg, session)

        if net is probe.State.ONLINE:
            state.clear(st)
            state.save(st, cfg.state_path)
            log.info("已在线，无需操作")
            return 0

        if net in (probe.State.PORTAL_DOWN, probe.State.UNKNOWN):
            # 这两种情况登录注定失败，而每次失败都在累积锁号风险，不能白试
            log.warning("网络层问题（%s），跳过登录", _status(net))
            return 0

        if state.is_stopped(st, now, cfg.window_hours):
            log.error("已进入停止自动登录状态，需人工登录一次才能恢复")
            notify.notify(
                "校园网认证",
                "连续失败次数过多，已暂停自动登录，避免账号被锁。\n"
                "请手动登录一次校园网，之后会自动恢复。",
            )
            return 1

        log.info("检测到未认证，开始登录")
        return do_login(cfg, session, st, now)


def run_login(cfg) -> int:
    now = time.time()
    st = state.load(cfg.state_path)
    with _new_session() as session:
        return do_login(cfg, session, st, now)


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
        print(f"\n注意：配置文件里的 username 是 {cfg.username}，请把它改成 {username}")
    return 0


def run_test_captcha(cfg) -> int:
    results = accuracy.measure(cfg.samples_dir)
    print(accuracy.report(results))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="login.py", description="校园网自动认证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="探测并按需登录，供计划任务调用")
    parser.add_argument("--login", action="store_true", help="强制登录一次，跳过探测")
    parser.add_argument("--setup", action="store_true", help="交互式保存账号密码")
    parser.add_argument("--test-captcha", action="store_true", help="统计验证码识别准确率")
    parser.add_argument("--config", default=None, help="配置文件路径，默认脚本同目录下的 config.json")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")

    args = parser.parse_args(argv)

    if not any([args.check, args.login, args.setup, args.test_captcha]):
        parser.print_help()
        return 2

    cfg_path = args.config or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

    # 先按配置文件所在目录初始化日志，这样连"配置文件读不出来"这种错也能落盘。
    # 计划任务跑起来时 stderr 是被丢掉的，不先开日志就等于什么都没留下。
    # 正常情况下 cfg.log_path 与这个路径相同，下面的第二次 init 会被短路径判断短路掉。
    fallback_log = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "campus-net.log")

    # 这次的 init 也要包在 try 里：日志目录不可写（只读盘、权限、路径某段是文件）
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
        if args.test_captcha:
            return run_test_captcha(cfg)
        if args.login:
            return run_login(cfg)
        return run_check(cfg)
    except Exception:
        log.exception("未预期的错误")
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_cli.py -v
```

Expected: 8 passed

- [ ] **Step 5: 跑全部测试**

```bash
python -m pytest -v
```

Expected: 全部通过

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: CLI 编排、重试与防锁号，退出码约定"
```

---

## Task 14: 配置样例与首次真机联调

**Files:**
- Create: `config.json`
- Create: `config.example.json`

- [ ] **Step 1: 写 config.example.json**

```json
{
  "username": "你的学号",
  "portal": "http://10.71.29.181",

  "login_page_url": "http://10.71.29.181/eportal/InterFace.do?method=pageInfo",
  "login_submit_url": "http://10.71.29.181/eportal/InterFace.do?method=login",
  "captcha_url": "http://10.71.29.181/eportal/validcode",

  "captcha_max_retry": 5,
  "run_max_fail": 3,
  "window_hours": 6,
  "window_max_fail": 6,
  "probe_timeout": 5,
  "log_level": "INFO",

  "probe_urls": [
    {
      "url": "http://www.msftconnecttest.com/connecttest.txt",
      "expect_text": "Microsoft Connect Test"
    },
    {
      "url": "http://connect.rom.miui.com/generate_204",
      "expect_status": 204
    }
  ]
}
```

- [ ] **Step 2: 复制成 config.json 并填学号**

```bash
cp config.example.json config.json
```

然后把 `config.json` 里的 `username` 改成真实学号。

> `config.json` 已在 `.gitignore` 里吗？没有——它只含账号不含密码，入库无妨。但若你介意，把它也加进 `.gitignore`。

- [ ] **Step 3: 保存密码**

```bash
python login.py --setup
```

Expected: 提示输入密码（不回显），成功后打印 `密码已加密保存到: ...secret.dat`

- [ ] **Step 4: 验证密码文件不含明文**

```bash
python -c "d=open('secret.dat','rb').read(); print('大小', len(d), '字节')"
```

Expected: 打印几十到几百字节。**若能在文件里搜到密码明文，立刻停下检查。**

- [ ] **Step 5: 先导测试——验证码准确率**

先把 30 张验证码图片存进 `samples/`，文件名写成图片里的字符（如 `3f7k.png`），然后：

```bash
python login.py --test-captcha
```

Expected: 打印样本数、准确率、错误样本清单，以及路线判定。按判定结果决定是否调整 `captcha_max_retry`。

- [ ] **Step 6: 真机登录一次**

在**未认证状态**下（先注销或断网重连，让登录页弹出来）：

```bash
python login.py --login
```

Expected: 日志显示"登录成功"，然后能正常上网。查 `campus-net.log` 看过程。

**注意**：这一步失败很正常（多半是 Task 12 的字段校对没做完）。**失败时不要反复重跑**，改完再试，避免触发风控。

- [ ] **Step 7: 探测路径联调**

注销校园网认证，保持在线状态（这样探测应判定为 UNAUTHED）：

```bash
python login.py --check
```

Expected: 日志依次出现"检测到未认证，开始登录"和"登录成功"，退出码 0。

- [ ] **Step 8: 已验证状态下应静默**

```bash
python login.py --check; echo "退出码: $?"
```

Expected: 日志显示"已在线，无需操作"，退出码 0，不发起任何登录。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: 配置样例与首次真机联调通过"
```

---

## Task 15: install_task.ps1 — 计划任务

**Files:**
- Create: `install_task.ps1`
- Create: `uninstall_task.ps1`

触发器（设计文档第 6 节）：登录时 + 每 30 分钟 + 工作站解锁 + 网络配置变更。

- [ ] **Step 1: 写 install_task.ps1**

> Windows PowerShell 5.1 的 `New-ScheduledTaskTrigger` 不支持"工作站解锁"和"网络配置变更"触发器，所以这里直接写 XML 注册，不绕 cmdlet。

```powershell
<#
注册校园网自动认证的计划任务。

设计取舍：不做常驻进程，每 30 分钟拉起脚本一次，外加关键时刻立即触发。
好处：崩了下一轮自动恢复；改代码立即生效；不需要守护逻辑。

用法（普通权限即可）：
  powershell -ExecutionPolicy Bypass -File install_task.ps1
#>
$ErrorActionPreference = "Stop"

$TaskName = "CampusNetAutoLogin"
$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script   = Join-Path $Root "login.py"

if (-not (Test-Path $Script)) { throw "找不到 login.py: $Script" }

$Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $Python) { throw "PATH 里找不到 python.exe" }

Write-Host "Python : $Python"
Write-Host "脚本   : $Script"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$start = (Get-Date).AddMinutes(2).ToString("yyyy-MM-ddTHH:mm:ss")

# 直接用 XML 定义，因为"工作站解锁"和"网络配置变更"触发器
# 在 PowerShell 5.1 的 cmdlet 里没有对应参数。
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>校园网自动认证：定时探测，未认证时自动登录</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$env:USERDOMAIN\$env:USERNAME</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <StartBoundary>$start</StartBoundary>
      <Repetition>
        <Interval>PT30M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>SessionUnlock</StateChange>
      <UserId>$env:USERDOMAIN\$env:USERNAME</UserId>
    </SessionStateChangeTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"&gt;&lt;Select Path="Microsoft-Windows-NetworkProfile/Operational"&gt;*[System[EventID=10000]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
      <Delay>PT10S</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$env:USERDOMAIN\$env:USERNAME</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT3M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$Python</Command>
      <Arguments>"$Script" --check</Arguments>
      <WorkingDirectory>$Root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $TaskName -Xml $xml | Out-Null

Write-Host "`n已注册计划任务: $TaskName"
Write-Host "触发器: 登录时 / 每 30 分钟 / 工作站解锁 / 网络配置变更"
Write-Host "`n查看:  schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "手动跑: schtasks /Run /TN $TaskName"
Write-Host "删除:  powershell -ExecutionPolicy Bypass -File uninstall_task.ps1"
```

- [ ] **Step 2: 写 uninstall_task.ps1**

```powershell
$ErrorActionPreference = "Stop"
$TaskName = "CampusNetAutoLogin"
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已删除计划任务: $TaskName"
```

- [ ] **Step 3: 注册**

```bash
cd "/c/Users/De Ronaldinho/campus-net"
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

Expected: 打印 Python 路径、脚本路径，以及"已注册计划任务"。

- [ ] **Step 4: 验证触发器齐全**

```bash
schtasks /Query /TN CampusNetAutoLogin /V /FO LIST | head -40
```

Expected: 能看到任务的"下次运行时间"和状态"就绪"。

- [ ] **Step 5: 手动触发一次**

```bash
schtasks /Run /TN CampusNetAutoLogin
sleep 8
tail -20 "/c/Users/De Ronaldinho/campus-net/campus-net.log"
```

Expected: 日志里有本次运行的记录。已在线时应该是"已在线，无需操作"。

- [ ] **Step 6: 验证弹窗在计划任务下可见**

把 `config.json` 里的 `probe_urls` 临时改成指向一个必定超时的地址，让探测判定为 UNAUTHED；再把 `captcha_max_retry` 改成 1，然后：

```bash
schtasks /Run /TN CampusNetAutoLogin
```

Expected: 几秒后屏幕上弹出验证码窗口（说明弹窗路径在计划任务下确实可见）。

> 验证完**务必改回** `probe_urls` 和 `captcha_max_retry`。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: 计划任务注册脚本，四种触发器"
```

---

## Task 16: 收尾

**Files:**
- Create: `README.md`

- [ ] **Step 1: 写 README.md**

内容包含：一句话说明、安装步骤（`pip install -r requirements.txt` → `--setup`）、常用命令（`--check` / `--login` / `--setup` / `--test-captcha`）、日志位置、如何卸载计划任务、以及"本工具仅供本人本机使用"的说明。

- [ ] **Step 2: 跑全部测试与自查**

```bash
cd "/c/Users/De Ronaldinho/campus-net"
python -m pytest -v
git status
```

Expected: 全部测试通过，工作区干净。

- [ ] **Step 3: 观察几天**

按设计文档第 10 节，实机观察日志，按实际表现调 `captcha_max_retry` 和轮询间隔。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs: README"
```

---

## 已覆盖的设计要求自查

| 设计文档要求 | 对应任务 |
|---|---|
| 四态探测、PORTAL_DOWN/UNKNOWN 不触发登录 | Task 6、Task 13 |
| 不做常驻进程，定时拉起 | Task 15 |
| 30 分钟轮询 + 开机/解锁/网络切换触发 | Task 15 |
| 同一 Session 取验证码与提交 | Task 9（有专门测试） |
| OCR 主力 + 弹窗兜底 | Task 8、Task 10、Task 13 |
| 单次运行内失败 3 次即停 | Task 13 `do_login` |
| 跨运行 6 小时窗口 6 次即停 | Task 7、Task 13 |
| 账号密码错误不重试 | Task 9 `_classify`、Task 13 |
| DPAPI 存密码、日志不打印密码 | Task 5、Task 9 |
| 先导测试决定路线 | Task 11、Task 14 |
| 提交端点可配置、隔离在一个函数 | Task 4、Task 9、Task 12 |
| 退出码 0/1/2 | Task 13 |
| 弹窗在无桌面会话时不崩 | Task 10 |
| 不做 GUI/托盘/多账号 | 全计划未涉及 |
