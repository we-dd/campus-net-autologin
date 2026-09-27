"""browser_login 的纯函数层。

这里一个浏览器都不起：字段判定全部由 pick_* / locate 这几个纯函数负责，
描述符就是普通 dict，手搓出各种门户页面即可。

这个切分是刻意的——门户改版时最先坏的就是这一层，而它恰好不需要浏览器就能测。
"""
import logging
import time
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import (
    InvalidSelectorException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)

from src import browser_login
from src.browser_login import (
    REASONS,
    _captcha_rank,
    _describe,
    _find_by_selector,
    _refresh_captcha,
    _tick_agree,
    decide_submit,
    judge,
    locate,
    pick_agree,
    pick_captcha_image,
    pick_captcha_input,
    pick_captcha_reload,
    pick_password,
    pick_submit,
    pick_username,
    render_elements,
    render_overrides,
)


# --- 描述符构造 -------------------------------------------------------------


def _desc(tag, index, **kw):
    """造一个描述符。字段名和 _describe() 的输出保持一致。"""
    d = {
        "index": index, "tag": tag, "type": "", "id": "", "name": "", "class": "",
        "placeholder": "", "value": "", "src": "", "text": "", "nearby": "",
        "visible": True, "size": (0, 0),
    }
    if "class_" in kw:  # class 是 Python 关键字，测试里用 class_ 传
        kw["class"] = kw.pop("class_")
    d.update(kw)
    return d


def _text_input(index, **kw):
    kw.setdefault("size", (140, 21))
    return _desc("input", index, type="text", **kw)


def _password_input(index, **kw):
    kw.setdefault("size", (140, 21))
    return _desc("input", index, type="password", **kw)


def _image(index, **kw):
    return _desc("img", index, **kw)


def _submit(index, **kw):
    kw.setdefault("size", (60, 24))
    kw.setdefault("text", "登录")
    return _desc("button", index, type="submit", **kw)


def _split(descriptors):
    """把描述符列表分成 (控件, 图片)，和 _collect 之后调用方的分法一样。"""
    controls = [d for d in descriptors if d["tag"] != "img"]
    images = [d for d in descriptors if d["tag"] == "img"]
    return controls, images


# --- 驱动层的测试替身（不起浏览器）------------------------------------------


class _StubElement:
    """给驱动层那几个函数用的元素替身。

    只实现被真正调用的几个接口：find_element（找可点击祖先）、is_selected、
    is_displayed、clear。用身份比较来断言"点的是哪一个"，所以不需要更多行为。
    """

    tag_name = "input"
    size = {"width": 10, "height": 10}
    text = ""

    def __init__(self, name, ancestor=None, selected=False, displayed=True,
                 attrs=None):
        self.name = name
        self._ancestor = ancestor
        self._selected = selected
        self._displayed = displayed
        self._attrs = {"type": "text", "id": name}
        self._attrs.update(attrs or {})
        self.cleared = False

    def get_attribute(self, attr):
        return self._attrs.get(attr)

    def find_element(self, by, value):
        if self._ancestor is None:
            raise NoSuchElementException(value)
        return self._ancestor

    def is_selected(self):
        return self._selected

    def is_displayed(self):
        return self._displayed

    def clear(self):
        self.cleared = True

    def __repr__(self):
        return f"<{self.name}>"


class _StaleElement:
    """一碰就 stale 的元素：模拟"页面在采集过程中重渲染"。

    size / tag_name 是本地属性不会抛，属性读取和可见性判断会抛
    StaleElementReferenceException —— 跟真实情况一样。
    """

    tag_name = "input"
    size = {"width": 10, "height": 10}

    def get_attribute(self, name):
        raise StaleElementReferenceException("stale element reference")

    def is_displayed(self):
        raise StaleElementReferenceException("stale element reference")


class _StubDriver:
    """假 driver：记录 JS 点击/整页刷新/脚本调用，并按配置回答 find_elements、
    readyState 和 current_url。
    """

    def __init__(self, found=None, js_ok=True, ready_state="complete",
                 changing=False, attached=True):
        self.js_clicks = []
        self.refreshes = 0
        self.script_calls = []
        self.ready_state = ready_state
        self._found = [] if found is None else found
        self._js_ok = js_ok
        self._attached = attached
        self._changing = changing
        self._seq = 0

    @property
    def current_url(self):
        """changing=True 时每次读都换一个地址，用来模拟"跳转链还没走完"。"""
        if not self._changing:
            return "http://portal/login"
        self._seq += 1
        return f"http://portal/step{self._seq}"

    def find_elements(self, by, value):
        if isinstance(self._found, Exception):
            raise self._found
        return list(self._found)

    def execute_script(self, script, *args):
        self.script_calls.append(script)
        if "readyState" in script:
            return self.ready_state
        if not self._js_ok:
            raise WebDriverException("blocked")
        if ".click()" in script:
            self.js_clicks.append(args[0])
            return None
        # document.contains 之类的探针
        return self._attached

    def refresh(self):
        self.refreshes += 1


class _StubDumpDriver:
    """给 _dump_failure 用的假 driver：记录清空动作和截图路径，吐预置的页面源码。"""

    def __init__(self, source="", password_fields=(), screenshot_ok=True):
        self.page_source = source
        self._password_fields = list(password_fields)
        self._screenshot_ok = screenshot_ok
        self.screenshot_path = None

    def find_elements(self, by, value):
        return self._password_fields

    def save_screenshot(self, path):
        self.screenshot_path = path
        if self._screenshot_ok:
            with open(path, "wb") as f:
                f.write(b"\x89PNG fake")
        return self._screenshot_ok


# --- 真实门户页面的形状 ------------------------------------------------------


def ruijie_page():
    """典型的锐捷 ePortal 登录页。"""
    return [
        _text_input(0, id="username", name="username", placeholder="请输入学号"),
        _password_input(1, id="password", name="password"),
        _image(2, id="validcodeImg", src="/eportal/validcode", size=(90, 34)),
        _text_input(3, id="validcode", name="validcode", size=(90, 21)),
        _submit(4),
    ]


def search_page():
    """不是登录页：门户首页 / 搜索页，压根没有密码框。"""
    return [
        _text_input(0, id="search", name="q", placeholder="搜索"),
        _desc("button", 1, type="button", text="搜索", size=(40, 24)),
        _image(2, id="logo", src="/static/logo.png", size=(420, 120)),
        _desc("a", 3, text="首页", size=(30, 16)),
    ]


def no_captcha_page():
    """没有验证码的门户：唯一的图片是校徽，尺寸远大于验证码。"""
    return [
        _text_input(0, id="username", name="username"),
        _password_input(1, id="password", name="password"),
        _image(2, id="logo", src="/static/school-logo.png", size=(420, 120)),
        _submit(3),
    ]


def two_text_inputs_page():
    """两个文本框，第二个才是验证码（任务书点名的场景）。"""
    return [
        _text_input(0, id="username", name="username"),
        _password_input(1, id="password", name="password"),
        _image(2, id="code_img", src="/code.php", size=(80, 30)),
        _text_input(3, id="input_code", name="input_code"),
        _submit(4),
    ]


def captcha_image_first_page():
    """验证码图片排在用户名左边。

    这种页面上"离图片最近的文本框"就是用户名框——不把已占用的字段排除掉，
    验证码会被打进学号里，然后每次都因为验证码为空而失败。
    """
    return [
        _image(0, id="validcodeImg", src="/eportal/validcode", size=(90, 34)),
        _text_input(1, id="username", name="username"),
        _password_input(2, id="password", name="password"),
        _text_input(3, id="validcode", name="validcode"),
        _submit(4),
    ]


def small_logo_page():
    """密码框旁边有个 32x32 的小 logo，真正的验证码图在更右边。

    只按"离密码框最近"挑的话会挑中小 logo，所以必须让名字里的 captcha 字样优先。
    """
    return [
        _text_input(0, id="username", name="username"),
        _password_input(1, id="password", name="password"),
        _image(2, id="logo", src="/static/logo.png", size=(32, 32)),
        _image(3, id="validcodeImg", src="/eportal/validcode", size=(90, 34)),
        _text_input(4, id="validcode", name="validcode"),
        _desc("button", 5, type="submit", text="登录"),
    ]


# --- 字段定位 ---------------------------------------------------------------


def test_locates_every_field_on_a_typical_portal_page():
    controls, images = _split(ruijie_page())
    page = locate(controls, images)

    assert page.reason == ""
    assert page.username["index"] == 0
    assert page.password["index"] == 1
    assert page.captcha_image["index"] == 2
    assert page.captcha_input["index"] == 3
    assert page.submit["index"] == 4


def test_username_is_the_first_text_input_not_the_captcha_box():
    """用户名必须是第一个文本框：第二个文本框是验证码，不是用户名。"""
    controls, images = _split(two_text_inputs_page())
    page = locate(controls, images)

    assert page.username["id"] == "username"
    assert page.captcha_input["id"] == "input_code"
    assert page.captcha_image["id"] == "code_img"


def test_captcha_box_excludes_username_even_when_the_image_is_left_of_it():
    controls, images = _split(captcha_image_first_page())
    page = locate(controls, images)

    assert page.username["index"] == 1
    assert page.captcha_image["index"] == 0

    # 离图片最近的是 index=1 的用户名框，但它已经被占用了，必须跳过
    taken = {page.username["index"], page.password["index"]}
    assert pick_captcha_input(controls, page.captcha_image, taken)["index"] == 3


def test_captcha_image_prefers_name_hint_over_being_closer():
    controls, images = _split(small_logo_page())
    page = locate(controls, images)

    assert page.captcha_image["index"] == 3          # validcodeImg，带关键字
    assert page.captcha_input["index"] == 4


def test_large_logo_is_never_taken_for_a_captcha():
    controls, images = _split(no_captcha_page())
    assert pick_captcha_image(images, pick_password(controls)) is None


def test_no_captcha_at_all_still_proceeds():
    """页面没有验证码时不该卡住：图片和输入框都没找到，按"无需验证码"继续。"""
    controls, images = _split(no_captcha_page())
    page = locate(controls, images)

    assert page.reason == ""
    assert page.captcha_image is None
    assert page.captcha_input is None
    assert page.submit["index"] == 3


def test_hidden_or_zero_size_image_is_not_a_captcha():
    assert pick_captcha_image(
        [_image(0, id="validcodeImg", src="/validcode", size=(90, 34), visible=False)], None
    ) is None
    assert pick_captcha_image(
        [_image(0, id="validcodeImg", src="/validcode", size=(0, 0))], None
    ) is None


def test_only_half_a_captcha_is_reported_instead_of_guessed():
    """只找到验证码图片、没找到输入框时，宁可放弃这一轮。

    用同样的找法重试只会重复同样的失败，而每次失败都在消耗锁号预算。
    """
    controls, images = _split([
        _text_input(0, id="username"),
        _password_input(1, id="password"),
        _image(2, id="validcodeImg", src="/validcode", size=(90, 34)),
        _submit(3),
    ])
    page = locate(controls, images)

    assert page.reason == "not_login_page"
    assert "验证码" in page.detail


# --- 不是登录页 -------------------------------------------------------------


def test_page_without_password_field_is_not_a_login_page():
    controls, images = _split(search_page())
    page = locate(controls, images)

    assert page.password is None
    assert page.reason == "not_login_page"
    assert "密码输入框" in page.detail


def test_page_without_username_field_is_not_a_login_page():
    controls, images = _split([_password_input(0, id="password")])
    page = locate(controls, images)

    assert page.username is None
    assert page.reason == "not_login_page"


def test_hidden_password_field_is_not_a_login_page():
    """隐藏的密码框不算：它可能是模板里没删掉的残留，页面上并没有表单。"""
    controls, images = _split([
        _text_input(0, id="username"),
        _password_input(1, id="password", visible=False),
    ])
    assert locate(controls, images).reason == "not_login_page"


def test_not_login_page_detail_tells_the_user_how_to_get_the_element_list():
    """detail 是给非程序员看的，必须指出下一步动作（--dump-form）。"""
    controls, images = _split(search_page())
    assert "--dump-form" in locate(controls, images).detail


# --- 提交按钮 ---------------------------------------------------------------


def test_submit_is_the_explicit_button_by_default():
    controls, _ = _split(ruijie_page())
    assert pick_submit(controls)["index"] == 4


def test_submit_prefers_explicit_control_over_link_text():
    controls = [
        _desc("a", 0, text="登录", size=(30, 16)),
        _desc("input", 1, type="submit", value="登录", size=(60, 24)),
    ]
    assert pick_submit(controls)["index"] == 1


def test_submit_falls_back_to_element_text():
    """有的门户用 <a>/<div> 当按钮，文字才是唯一的线索。"""
    controls = [
        _text_input(0, id="username"),
        _desc("a", 1, class_="btn", text="立即登录", size=(80, 30)),
    ]
    assert pick_submit(controls)["index"] == 1


def test_submit_absent_returns_none_so_the_caller_uses_enter():
    assert pick_submit([_text_input(0), _password_input(1)]) is None


def test_hidden_submit_is_ignored():
    controls = [_submit(0, visible=False), _desc("a", 1, text="登录", size=(30, 16))]
    assert pick_submit(controls)["index"] == 1


# --- 成功判定 ---------------------------------------------------------------


def _snapshot(**kw):
    snap = {"url": "http://portal/", "has_password": True,
            "has_username": True, "body_text": ""}
    snap.update(kw)
    return snap


def test_judge_success_when_the_login_form_is_gone():
    ok, reason, _ = judge(_snapshot(has_password=False, has_username=False,
                                    body_text="认证成功"))
    assert ok is True
    assert reason == "success"


def test_judge_reports_credentials_error_and_marks_it_not_retryable():
    ok, reason, _ = judge(_snapshot(body_text="您输入的密码错误，请重新输入"))
    assert ok is False
    assert reason == "bad_credentials"


def test_judge_reports_captcha_error_as_retryable():
    ok, reason, _ = judge(_snapshot(body_text="验证码错误，请重新输入"))
    assert ok is False
    assert reason == "captcha_failed"


def test_judge_form_still_present_means_failed_and_retryable():
    """验证码认错时门户留在登录页、只换一张图，这是最常见的失败形态。"""
    ok, reason, _ = judge(_snapshot(body_text="统一身份认证"))
    assert ok is False
    assert reason == "captcha_failed"


def test_judge_ignores_a_hidden_password_box():
    """成功页面上可能留着一个隐藏的密码框（模板没删干净），
    is_displayed 为 False 时不该被当成"表单还在"。"""
    ok, reason, _ = judge(_snapshot(has_password=False, has_username=False))
    assert ok is True


def test_judge_does_not_confuse_the_word_captcha_on_a_normal_page():
    """登录页上本来就写着"验证码"三个字，不能把它当成错误提示。"""
    ok, reason, _ = judge(_snapshot(body_text="学号 密码 验证码 登录"))
    assert ok is False
    assert reason == "captcha_failed"  # 是"表单还在"，不是"验证码错误"


def test_judge_treats_a_page_without_a_password_box_as_success():
    """登录成功后门户常跳回学校主页，那上面有搜索框。

    把这种页面判成"表单还在"就会继续提交，而那些提交会真的打到认证服务器上、
    消耗锁号预算；判成成功的代价只是 30 分钟后重试一次。
    """
    ok, reason, _ = judge(_snapshot(has_password=False, has_username=True,
                                    body_text="校园网自助服务"))
    assert ok is True
    assert reason == "success"


def test_every_reason_the_decisions_emit_is_declared():
    """REASONS 是 reason 的全集清单（同时是给读日志的人看的说明）。

    以后随手加一种新 reason 却忘了写进清单时，这条会提醒一次。
    """
    for snapshot in (_snapshot(has_password=False, has_username=False),
                     _snapshot(body_text="您输入的密码错误"),
                     _snapshot()):
        assert judge(snapshot)[1] in REASONS

    assert locate(*_split(search_page())).reason in REASONS


# --- 识别不出验证码时不要提交 -----------------------------------------------


def test_decide_submit_skips_the_submit_when_the_captcha_was_not_recognized():
    """空验证码提交出去是必然失败的，但会真的打到认证服务器、消耗一次锁号预算。"""
    submit, why = decide_submit(True, "")
    assert submit is False
    assert "没有提交" in why


def test_decide_submit_proceeds_when_the_captcha_was_recognized():
    assert decide_submit(True, "A7B3") == (True, "")


def test_decide_submit_proceeds_when_the_page_has_no_captcha():
    """没有验证码的页面不受影响，否则那种门户永远登不上。"""
    assert decide_submit(False, "") == (True, "")


# --- --dump-form 的输出 -----------------------------------------------------


def test_render_elements_lists_the_fields_needed_for_calibration():
    text = render_elements([
        _text_input(0, id="username", name="username", placeholder="请输入学号"),
        _submit(1),
    ])

    for token in ("input", "text", "username", "请输入学号", "140x21", "可见=是", "登录"):
        assert token in text


def test_render_elements_says_so_when_the_page_is_empty():
    assert "没有找到" in render_elements([])


# --- _describe 和 pick_* 的键必须对得上 -------------------------------------


class _FakeElement:
    """只实现 _describe 用到的那几个接口，用来免浏览器地测那一层。"""

    def __init__(self, tag, attrs, displayed=True, size=None, text=""):
        self.tag_name = tag
        self._attrs = attrs
        self._displayed = displayed
        self.size = size if size is not None else {"width": 90, "height": 34}
        self.text = text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_displayed(self):
        return self._displayed


def _described(elements):
    descriptors = []
    for index, el in enumerate(elements):
        desc = _describe(el)
        desc["index"] = index
        descriptors.append(desc)
    return descriptors


def test_describe_normalizes_the_attribute_names_and_case():
    desc = _describe(_FakeElement("INPUT", {
        "type": "TEXT", "id": "username", "name": "username",
        "class": "form-control", "placeholder": "学号",
    }, size={"width": 140, "height": 21}))

    assert desc["tag"] == "input"    # 门户写大写标签名也一样
    assert desc["type"] == "text"
    assert desc["class"] == "form-control"
    assert desc["size"] == (140, 21)
    assert desc["visible"] is True


def test_describe_tolerates_missing_attributes():
    desc = _describe(_FakeElement("IMG", {}, size=None))
    assert desc["id"] == ""
    assert desc["type"] == ""
    assert desc["visible"] is True


def test_render_overrides_says_hit_or_miss():
    """用户配完选择器后，唯一的确认办法就是再跑一次 --dump-form，
    所以命中/没找到/留空三种情况都必须说清楚。"""
    driver = _StubDriver(found=[_StubElement("hit")])

    text = render_overrides(driver, {"username": "#user"})
    assert "#user → 命中" in text

    text = render_overrides(_StubDriver(found=[]),
                            {"username": "#user", "password": "#nope", "submit": ""})
    assert "#nope → 没找到" in text
    assert "退回自动识别" in text
    assert "留空" in text


def test_render_overrides_says_so_when_nothing_is_configured():
    assert "没有配置 selectors" in render_overrides(None, {})


def test_typing_the_password_does_not_leak_it_through_the_exception():
    """send_keys 失败时，WebDriver 的异常消息里可能带着元素当前的值，也就是密码。

    这个异常会一路冒到 login() 的兜底 except 并被 log.exception 写进日志文件，
    所以必须在源头就把消息换掉。
    """

    class _El:
        def clear(self):
            pass

        def send_keys(self, value):
            raise WebDriverException(f"element not interactable: value={value}")

    with pytest.raises(browser_login._PasswordSendError) as excinfo:
        browser_login._type_password(_El(), "Sup3rSecret!DoNotLog")

    assert "Sup3rSecret" not in str(excinfo.value)


def test_press_enter_does_not_leak_the_password_through_the_exception():
    """回车提交也是在密码框上按的，所以是同一个泄漏路径。"""

    class _El:
        def send_keys(self, keys):
            raise WebDriverException("element not interactable: value=Sup3rSecret!DoNotLog")

    with pytest.raises(browser_login._PasswordSendError) as excinfo:
        browser_login._press_enter(_El())

    assert "Sup3rSecret" not in str(excinfo.value)


def test_settle_returns_once_the_url_stops_changing(monkeypatch):
    """地址连续几次采样都不变，就算稳定了。

    这一步修的是真机那次崩溃：导航后 0.5 秒就去采集元素，采到一半 Angular 把整页
    重渲染了一遍，手里所有句柄一起失效。
    """
    monkeypatch.setattr(browser_login, "_SETTLE_STEP", 0)
    driver = _StubDriver()

    assert browser_login._wait_for_settle(driver, time.monotonic() + 5, 5) is True
    assert any("readyState" in script for script in driver.script_calls)


def test_settle_gives_up_with_a_clear_log_when_the_url_keeps_changing(monkeypatch, caplog):
    """地址一直在变（跳转链还没走完）时要按时放弃，并且日志要说明白是"没等到稳定"，
    而不是留下一句莫名其妙的失败。"""
    monkeypatch.setattr(browser_login, "_SETTLE_STEP", 0)
    driver = _StubDriver(changing=True)

    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        ok = browser_login._wait_for_settle(driver, time.monotonic() + 0.01, 20)

    assert ok is False
    assert "等待页面稳定超时" in caplog.text
    assert "readyState" in caplog.text


def test_settle_waits_while_the_document_is_still_loading(monkeypatch):
    monkeypatch.setattr(browser_login, "_SETTLE_STEP", 0)
    driver = _StubDriver(ready_state="loading")
    assert browser_login._wait_for_settle(driver, time.monotonic() + 0.01, 20) is False


def test_ready_wait_still_settles_even_when_a_password_selector_is_configured(monkeypatch):
    """配了 password 选择器只是跳过"等密码框"这一步。页面稳不稳定跟有没有选择器
    毫无关系，所以等稳定**不能**一起跳过——真机那次就是没等稳定才崩的。"""
    monkeypatch.setattr(browser_login, "_SETTLE_STEP", 0)
    monkeypatch.setattr(browser_login, "_FORM_WAIT_STEP", 0)
    driver = _StubDriver(found=[])          # 页面上一个元素都没有

    browser_login._wait_until_ready(
        driver, SimpleNamespace(selectors={"password": "#pwd"}), time.monotonic() + 5, 5)

    assert any("readyState" in script for script in driver.script_calls)


def test_ready_wait_waits_for_the_password_field_when_nothing_is_configured(monkeypatch, caplog):
    monkeypatch.setattr(browser_login, "_SETTLE_STEP", 0)
    monkeypatch.setattr(browser_login, "_FORM_WAIT_STEP", 0)
    driver = _StubDriver(found=[])

    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        browser_login._wait_until_ready(
            driver, SimpleNamespace(selectors={}), time.monotonic() + 0.01, 20)

    assert "等不到登录表单" in caplog.text


def test_build_driver_silences_the_loggers_that_would_leak_the_password(monkeypatch):
    """selenium 的 remote_connection 在 DEBUG 级别会把 send_keys 的内容原样写进日志，
    而输入密码走的就是 send_keys。用户排查登录问题时必然开 --verbose（即 DEBUG），
    所以这条泄漏路径一定会被走到：实测日志里出现过

        POST http://localhost:.../element/.../value {'text': '<密码明文>', ...}

    这里用假的 driver 工厂测接线（不起真浏览器），钉住"启动前必须先把它们关掉"。
    """

    class _FakeDriver:
        def set_page_load_timeout(self, seconds):
            self.timeout = seconds

    monkeypatch.setattr(browser_login.webdriver, "Edge",
                        lambda options=None: _FakeDriver())

    leaked = logging.getLogger("selenium.webdriver.remote.remote_connection")
    root = logging.getLogger("selenium")
    before = root.level
    root.setLevel(logging.NOTSET)  # 先放开，确认是 _build_driver 把它关掉的
    try:
        browser_login._build_driver(
            SimpleNamespace(browser="edge", headless=True, probe_timeout=7),
            "C:/tmp/profile",
        )
        assert leaked.isEnabledFor(logging.DEBUG) is False
        assert leaked.getEffectiveLevel() >= logging.WARNING
    finally:
        root.setLevel(before)


def test_describe_output_feeds_locate():
    """把 _describe 的输出直接喂给 locate。

    这两层是分开写的：_describe 少一个键（比如把 size 写成 dimensions），
    后果是静默退化成"什么都找不到"，而不是报错。所以要有一条用例把它们扣在一起。
    """
    descriptors = _described([
        _FakeElement("INPUT", {"type": "text", "id": "username"}),
        _FakeElement("INPUT", {"type": "password", "id": "password"}),
        _FakeElement("IMG", {"id": "validcodeImg", "src": "/eportal/validcode"},
                     size={"width": 90, "height": 34}),
        _FakeElement("INPUT", {"type": "text", "id": "validcode"}),
        _FakeElement("BUTTON", {"type": "submit"}, text="登录"),
    ])
    controls, images = _split(descriptors)
    page = locate(controls, images)

    assert page.reason == ""
    assert page.username["index"] == 0
    assert page.password["index"] == 1
    assert page.captcha_image["index"] == 2
    assert page.captcha_input["index"] == 3
    assert page.submit["index"] == 4


# ===========================================================================
# 真实门户页面（四川工业科技学院 SAM SSO，Angular SPA）
#
# 下面这个 fixture 的属性值全部来自对真实页面的实测，不是编的：
#   - input 共 9 个，其中 2 个可见（账号、验证码），6 个 hidden（含一个和验证码框
#     同名的隐藏字段，就是它让 find_element("input[name=captcha_code]") 打偏）
#   - 图片 2 个：14x14 的"显示密码"眼睛图标、80x28 的真验证码 img.code-img
#   - 唯一的复选框是隐藏的（ant-design 把真实 input 藏了）
#   - "换一个"是 a.code-reload-content，并且它包住了验证码图片
# 只有各标签之间的交错顺序是按 Angular 表单的常理摆的（<a> 包住 <img>，故 <a> 在前）。
# ===========================================================================


def real_portal_page():
    return [
        _desc("input", 0, type="text", id="nameInput", name="username",
              class_="ant-input ng-untouched ng-pristine ng-valid ng-star-inserted",
              placeholder="请输入账号", size=(322, 34)),
        _desc("input", 1, type="password",
              class_="ant-input ng-untouched ng-pristine ng-valid ng-star-inserted",
              placeholder="请输入密码", size=(322, 34)),
        # 密码框的"显示密码"眼睛图标：只有 14x14，也过得了尺寸门槛，
        # 而且离密码框比真验证码近得多
        _desc("img", 2, class_="icon-password-hide ng-star-inserted",
              src="http://10.71.29.181/sam-sso-public/cas-login-new/assets/login/pwd-hide.svg",
              size=(14, 14)),
        _desc("input", 3, type="hidden", name="type", value="UsernamePassword",
              size=(0, 0), visible=False),
        _desc("input", 4, type="hidden", name="_eventId", value="submit",
              size=(0, 0), visible=False),
        _desc("input", 5, type="hidden", name="geolocation", size=(0, 0), visible=False),
        _desc("input", 6, type="hidden", name="execution",
              value="40cc1087-f4ae-44ce-9", size=(0, 0), visible=False),
        # 和验证码框同名、但是隐藏的那个（实测 find_element 会先返回它）
        _desc("input", 7, type="hidden", name="captcha_code", size=(0, 0), visible=False),
        _desc("input", 8, type="text", name="captcha_code",
              class_="ant-input ng-untouched ng-pristine ng-valid ng-star-inserted",
              placeholder="校验码", size=(237, 34)),
        _desc("a", 9, class_="code-reload-content ng-star-inserted", text="换一个",
              size=(40, 20)),
        _desc("img", 10, class_="code-img",
              src="http://10.71.29.181/sam-sso/api/captcha/generate/DEFAULT",
              size=(80, 28)),
        _desc("input", 11, type="checkbox",
              class_="ant-checkbox-input ng-untouched ng-pristine ng-valid",
              value="on", size=(16, 16), visible=False,
              nearby="请先阅读并同意《用户协议》《免责声明协议》《用户隐私政策》"),
        _desc("button", 12, type="button", class_="ant-tabs-nav-more ant-dropdown-trigger",
              visible=False, size=(46, 32)),
        _desc("button", 13, type="submit", id="submitBtn",
              class_="igniter--in-password theme-button--primary ant-btn disabled",
              text="立即登录", size=(370, 40)),
    ]


def test_real_portal_page_is_fully_located():
    """实测页面上的七个字段全部要能自动认出来（不依赖任何 selectors 覆盖）。"""
    controls, images = _split(real_portal_page())
    page = locate(controls, images)

    assert page.reason == ""
    assert page.username["id"] == "nameInput"
    assert page.password["index"] == 1
    assert page.captcha_image["index"] == 10            # img.code-img
    assert page.captcha_input["index"] == 8             # 可见的那个同名字段
    assert page.captcha_reload["index"] == 9            # a.code-reload-content
    assert page.agree["index"] == 11                    # 那个隐藏的复选框
    assert page.submit["index"] == 13                   # #submitBtn


def test_real_portal_captcha_image_beats_the_closer_password_eye_icon():
    """眼睛图标 14x14 也过尺寸门槛，而且比真验证码离密码框近得多。

    只按距离挑就会把眼睛图标截下来当验证码送去识别——这是一条真实的误判路径，
    所以这里把"档位压过距离"钉死。
    """
    controls, images = _split(real_portal_page())
    pwd = pick_password(controls)
    eye, captcha = images[0], images[1]

    assert _captcha_rank(eye) == 2                       # 一个关键字都不沾
    assert _captcha_rank(captcha) == 0                   # src 里含 captcha，强档
    assert abs(eye["index"] - pwd["index"]) < abs(captcha["index"] - pwd["index"])
    assert pick_captcha_image(images, pwd)["index"] == captcha["index"]


def test_real_portal_visible_captcha_input_wins_over_the_hidden_namesake():
    """name=captcha_code 有两个元素，隐藏的那个在前面。

    注意 _split 会把图片摘出去、位置会变，所以断言一律按描述符自己的 index 找，
    不按下标找。
    """
    controls, images = _split(real_portal_page())
    namesakes = [d for d in controls if d["name"] == "captcha_code"]
    hidden = next(d for d in namesakes if not d["visible"])
    visible = next(d for d in namesakes if d["visible"])

    image = pick_captcha_image(images, pick_password(controls))
    taken = {pick_username(controls)["index"], pick_password(controls)["index"]}
    chosen = pick_captcha_input(controls, image, taken)

    assert (hidden["index"], visible["index"]) == (7, 8)   # 隐藏的确实在前面
    assert chosen is visible


def test_real_portal_agree_box_is_found_even_though_it_is_hidden():
    """ant-design 把真实 input 藏起来了：按可见性过滤就永远找不到它。"""
    controls, _ = _split(real_portal_page())
    agree = pick_agree(controls)

    assert agree["index"] == 11
    assert agree["visible"] is False
    assert "同意" in agree["nearby"]


def test_real_portal_submit_is_the_visible_submit_button_not_the_hidden_tab_button():
    controls, _ = _split(real_portal_page())
    assert pick_submit(controls)["id"] == "submitBtn"


def test_real_portal_reload_control_is_the_wrapping_link():
    """实测点图片会被包着它的 <a> 拦掉，所以必须优先用它。"""
    controls, images = _split(real_portal_page())
    image = pick_captcha_image(images, pick_password(controls))
    reload_el = pick_captcha_reload(controls, image)

    assert reload_el["index"] == 9
    assert reload_el["class"].startswith("code-reload-content")
    assert "换一个" in reload_el["text"]


# --- 修复 1：选择器匹配到多个元素时要挑可见的那个 ---------------------------


def test_selector_prefers_the_first_visible_match():
    """实测：input[name=captcha_code] 在真实页面上匹配两个元素，第一个是隐藏的，
    而 find_element 返回的正是那个隐藏的——于是打字打进了虚空，验证码框始终是空的。"""
    hidden = _StubElement("hidden", displayed=False)
    visible = _StubElement("visible", displayed=True)
    driver = _StubDriver(found=[hidden, visible])

    chosen = _find_by_selector(driver, "input[name=captcha_code]", "captcha_input")

    assert chosen is visible


def test_selector_falls_back_to_the_first_match_when_none_are_visible():
    first = _StubElement("first", displayed=False)
    second = _StubElement("second", displayed=False)
    driver = _StubDriver(found=[first, second])

    assert _find_by_selector(driver, "input[name=x]", "captcha_input") is first


def test_selector_logs_how_many_matched_and_which_one_won(caplog):
    hidden = _StubElement("hidden", displayed=False)
    visible = _StubElement("visible", displayed=True)
    driver = _StubDriver(found=[hidden, visible])

    with caplog.at_level(logging.INFO, logger="src.browser_login"):
        _find_by_selector(driver, "input[name=captcha_code]", "captcha_input")

    assert "匹配到 2 个元素" in caplog.text
    assert "第 2 个" in caplog.text


def test_selector_with_a_single_match_is_returned_as_is():
    only = _StubElement("only")
    assert _find_by_selector(_StubDriver(found=[only]), "#submitBtn", "submit") is only


def test_selector_that_matches_nothing_warns_and_returns_none(caplog):
    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        got = _find_by_selector(_StubDriver(found=[]), "#nope", "submit")

    assert got is None
    assert "#nope" in caplog.text


def test_invalid_selector_warns_instead_of_raising(caplog):
    """用户把选择器写坏了（比如漏了半个引号）也只警告，不能把整轮登录弄崩。"""
    driver = _StubDriver(found=InvalidSelectorException("bad selector"))

    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        assert _find_by_selector(driver, "input[", "submit") is None

    assert "不是合法的选择器" in caplog.text


# --- 修复 2：不能依赖 CSS 的 [type=...] -------------------------------------


def test_username_is_found_when_the_descriptor_has_no_type_key():
    """实测：本页用户名框没有字面的 type 属性，CSS 的 input[type=text] 匹配不上
    （验证码框也一样）。描述符里 type 缺失时必须按"没有 type"（即 text）处理，
    而不是抛 KeyError。"""
    account = _desc("input", 0, id="nameInput", name="username",
                    placeholder="请输入账号", size=(322, 34))
    del account["type"]                                  # 完全没有 type 这个键
    controls, images = _split([
        account,
        _desc("input", 1, type="password", size=(322, 34)),
        _desc("button", 2, type="submit", text="立即登录"),
    ])

    assert "type" not in controls[0]
    assert locate(controls, images).username["id"] == "nameInput"


def test_type_selector_that_misses_explains_the_literal_attribute_trap(caplog):
    """用户写 [type=text] 又没匹配到时，提示里必须点出"字面的 type 属性"这个坑，
    否则他会以为是门户改版了。"""
    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        _find_by_selector(_StubDriver(found=[]), "input[type=text]", "username")

    assert "字面的 type 属性" in caplog.text


# --- 修复 3：同意协议勾选框 -------------------------------------------------


def test_agree_is_not_clicked_again_when_already_ticked():
    box = _StubElement("box", selected=True)
    driver = _StubDriver()

    _tick_agree(driver, box)

    assert driver.js_clicks == []


def test_agree_is_clicked_with_js_when_unticked():
    """实测：ant-design 的真实 input 是隐藏的，普通 click 点不到；
    JS 点击能让 is_selected() 由 False 变 True。"""
    box = _StubElement("box", selected=False)
    driver = _StubDriver()

    _tick_agree(driver, box)

    assert driver.js_clicks == [box]


def test_agree_missing_is_a_no_op_not_a_failure():
    driver = _StubDriver()

    _tick_agree(driver, None)

    assert driver.js_clicks == []


def test_agree_click_failure_only_warns(caplog):
    """勾不上只警告、继续提交，不能让整轮登录崩掉。"""
    box = _StubElement("box", selected=False)
    driver = _StubDriver(js_ok=False)

    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        _tick_agree(driver, box)

    assert "勾选用户协议失败" in caplog.text


def test_unmatched_agree_override_warns_and_falls_back_to_the_heuristic(caplog):
    """配的 agree 选择器匹配不到时：警告 + 退回自动识别，而不是失败。"""
    fallback = _StubElement("heuristic-box")
    driver = _StubDriver(found=[])

    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        got = browser_login._resolve(driver, [fallback], "input[type=checkbox]",
                                     {"index": 0}, "agree")

    assert got is fallback
    assert "agree" in caplog.text


def test_agree_override_is_used_when_it_matches():
    chosen = _StubElement("configured-box")
    heuristic = _StubElement("heuristic-box")
    driver = _StubDriver(found=[chosen])

    got = browser_login._resolve(driver, [heuristic], "#myBox", {"index": 0}, "agree")

    assert got is chosen


# --- 修复 4：验证码刷新要走 a.code-reload-content ----------------------------


def test_reload_control_is_preferred_over_the_captcha_image(monkeypatch):
    """实测点图片会被 ElementClickInterceptedException 拦掉，真正接收点击的是 <a>。"""
    monkeypatch.setattr(browser_login, "_REFRESH_WAIT", 0)
    image = _StubElement("image")
    reload_el = _StubElement("reload")
    driver = _StubDriver()

    _refresh_captcha(driver, reload_el, image)

    assert driver.js_clicks == [reload_el]
    assert driver.refreshes == 0


def test_refresh_falls_back_to_the_image_when_there_is_no_reload_control(monkeypatch):
    monkeypatch.setattr(browser_login, "_REFRESH_WAIT", 0)
    image = _StubElement("image")
    driver = _StubDriver()

    _refresh_captcha(driver, None, image)

    assert driver.js_clicks == [image]


def test_refresh_uses_the_images_clickable_ancestor(monkeypatch):
    """真实页面上 <a class="code-reload-content"> 正是 <img class="code-img"> 的祖先，
    所以"兜底点祖先"和"点刷新控件"其实是同一个元素。"""
    monkeypatch.setattr(browser_login, "_REFRESH_WAIT", 0)
    ancestor = _StubElement("a.code-reload-content")
    image = _StubElement("image", ancestor=ancestor)
    driver = _StubDriver()

    _refresh_captcha(driver, None, image)

    assert driver.js_clicks == [ancestor]


def test_refresh_falls_back_to_reloading_the_whole_page(monkeypatch):
    """什么都点不动时最后才整页刷新（它会把已填内容清掉，所以放最后）。"""
    monkeypatch.setattr(browser_login, "_REFRESH_WAIT", 0)
    image = _StubElement("image")
    driver = _StubDriver(js_ok=False)

    _refresh_captcha(driver, None, image)

    assert driver.js_clicks == []
    assert driver.refreshes == 1


def test_reload_control_absent_when_there_is_no_captcha_image():
    controls, _ = _split(real_portal_page())
    assert pick_captcha_reload(controls, None) is None


def test_report_paths_survive_a_missing_type_key():
    """诊断路径同样会读 type：判定"不是登录页"时的说明、--dump-form 的元素清单。

    这两条路径本来就是出错时才走的，在这里再抛一个 KeyError 会把真正的错因盖掉，
    用户看到的是崩溃而不是"没找到密码框（可运行 --dump-form 看清单）"。
    """
    controls, images = _split([_desc("input", 0, id="search", placeholder="搜索")])
    del controls[0]["type"]                              # 缺 type 键
    page = locate(controls, images)

    assert page.reason == "not_login_page"
    assert "密码输入框" in page.detail
    assert "type" in render_elements(controls)


# ===========================================================================
# 页面重渲染（stale element reference）
#
# 背景：第一次真机运行（真实门户，Angular SPA）以 stale 崩掉——导航后 0.5 秒去
# 采集元素，采到一半页面整页重渲染，手里所有句柄一起失效。下面这些用例钉住
# "不要把这类失败当成普通失败"和"不要把密码漏进现场转储"。
# ===========================================================================


def test_collect_refuses_a_snapshot_taken_while_the_page_re_rendered():
    """采集到一半元素失效时，清单是残缺的（失效的那些属性全空）。

    这种清单拿去判定会得出错误结论——"这页不是登录页"——比崩溃更难查。
    所以必须明确作废，让上层重来一轮。
    """
    driver = _StubDriver(found=[_StaleElement()])

    with pytest.raises(browser_login._PageChanged):
        browser_login._collect(driver)


def test_describe_marks_a_stale_element_instead_of_faking_empty_attributes():
    """失效的元素不能伪装成"属性全空但存在"的普通元素。"""
    desc = _describe(_StaleElement())

    assert desc.get("stale") is True
    assert "type" not in desc


def test_collect_is_fine_when_nothing_goes_stale():
    driver = _StubDriver(found=[_StubElement("ok")])
    elements, descriptors = browser_login._collect(driver)

    assert len(elements) == len(descriptors) == 1
    assert not any(d.get("stale") for d in descriptors)


def test_still_attached_asks_the_document():
    """用一次 JS 调用判断句柄还挂着没有，比重新采集一遍便宜得多。"""
    assert browser_login._still_attached(
        _StubDriver(attached=True), _StubElement("e")) is True
    assert browser_login._still_attached(
        _StubDriver(attached=False), _StubElement("e")) is False


def test_still_attached_treats_a_stale_handle_as_gone():
    """句柄已经 stale 时 execute_script 本身就会抛——那也是"不可用"。"""
    assert browser_login._still_attached(
        _StubDriver(js_ok=False), _StubElement("e")) is False


def test_require_alive_raises_page_changed_for_a_dead_handle():
    with pytest.raises(browser_login._PageChanged):
        browser_login._require_alive(
            _StubDriver(attached=False), _StubElement("e"), "账号输入框")


def test_require_alive_ignores_a_field_this_page_does_not_have():
    """el 为 None（本页没有这个字段）时不该报错。"""
    browser_login._require_alive(_StubDriver(attached=False), None, "同意协议勾选框")


# --- stale 必须保持"可重试"，不能被包成别的异常 -----------------------------


def test_typing_a_password_keeps_stale_retryable():
    """stale 必须原样往外抛。

    包成 _PasswordSendError 会让一次本来可以自愈的重渲染变成一次彻底失败的登录——
    因为上层只对 stale 做重试。
    """

    class _El:
        def clear(self):
            raise StaleElementReferenceException("stale")

        def send_keys(self, value):
            raise AssertionError("stale 之后不该再走到 send_keys")

    with pytest.raises(StaleElementReferenceException):
        browser_login._type_password(_El(), "pw")


def test_press_enter_keeps_stale_retryable():
    class _El:
        def send_keys(self, keys):
            raise StaleElementReferenceException("stale")

    with pytest.raises(StaleElementReferenceException):
        browser_login._press_enter(_El())


def test_fill_keeps_stale_retryable():
    class _El:
        def clear(self):
            raise StaleElementReferenceException("stale")

        def send_keys(self, value):
            raise AssertionError("stale 之后不该再走到 send_keys")

    with pytest.raises(StaleElementReferenceException):
        browser_login._fill(_El(), "value")


def _recorder(sink):
    """假的 captcha.recognize：把收到的图片字节记进 sink，并回一个非空结果。"""
    def _fake(png_bytes):
        sink["bytes"] = png_bytes
        return "abcd"
    return _fake


def test_solving_a_captcha_keeps_stale_retryable():
    """取图时图片失效也要重来一轮，不能当成"识别失败"——那会让 decide_submit
    跳过提交，等于白费一轮（而这一轮本来什么都没提交）。

    两条取图路径都要保持这一点：canvas 主路径和元素截图兜底路径。
    """

    class _StaleDriver:
        def execute_script(self, *a, **kw):
            raise StaleElementReferenceException("stale")

    class _ElCanvasStale:
        parent = _StaleDriver()

        @property
        def screenshot_as_png(self):        # 不该走到这里
            raise AssertionError("canvas 抛 stale 后不应再退回截图")

    class _ElShotStale:
        class parent:                        # canvas 不可用，走兜底
            def execute_script(self, *a, **kw):
                raise WebDriverException("canvas 不支持")

        @property
        def screenshot_as_png(self):
            raise StaleElementReferenceException("stale")

    with pytest.raises(StaleElementReferenceException):
        browser_login._solve_captcha(_ElCanvasStale())
    with pytest.raises(StaleElementReferenceException):
        browser_login._solve_captcha(_ElShotStale())


def test_captcha_prefers_native_resolution(monkeypatch):
    """主路径：用 canvas 按原生尺寸取图，而不是退化的元素截图。

    实测同一张图元素截图是 100x35、原生是 160x70，差约 3.2 倍像素。
    这里断言：canvas 给了数据就用 canvas 的，且解出来的字节原样交给 OCR。
    """
    import base64 as _b64

    payload = b"\x89PNG\r\n\x1a\n native-resolution-bytes"
    data_url = "data:image/png;base64," + _b64.b64encode(payload).decode()

    seen = {}

    class _Driver:
        def execute_script(self, script, el):
            return data_url

    class _El:
        parent = _Driver()

        @property
        def screenshot_as_png(self):
            raise AssertionError("canvas 可用时不该退回元素截图")

    monkeypatch.setattr(browser_login.captcha, "recognize", _recorder(seen))
    browser_login._solve_captcha(_El())
    assert seen["bytes"] == payload


def test_captcha_falls_back_to_element_screenshot(monkeypatch):
    """兜底：canvas 不可用时（抛 WebDriverException）退回元素截图，不能让整轮崩掉。"""
    shot = b"\x89PNG\r\n\x1a\n element-shot-bytes"
    seen = {}

    class _Driver:
        def execute_script(self, *a, **kw):
            raise WebDriverException("没有 canvas")

    class _El:
        parent = _Driver()

        @property
        def screenshot_as_png(self):
            return shot

    monkeypatch.setattr(browser_login.captcha, "recognize", _recorder(seen))
    browser_login._solve_captcha(_El())
    assert seen["bytes"] == shot


def test_captcha_falls_back_when_canvas_returns_junk(monkeypatch):
    """canvas 返回了不可解析的东西时，也要退回元素截图，而不是抛异常。"""
    shot = b"\x89PNG element-shot"
    seen = {}

    class _Driver:
        def execute_script(self, *a, **kw):
            return "这不是一个 data URL"

    class _El:
        parent = _Driver()

        @property
        def screenshot_as_png(self):
            return shot

    monkeypatch.setattr(browser_login.captcha, "recognize", _recorder(seen))
    browser_login._solve_captcha(_El())
    assert seen["bytes"] == shot


def test_submit_keeps_stale_retryable_and_does_not_fall_back_to_enter():
    """点提交时 stale 等价于"根本没提交"。

    WebDriver 会先解析元素引用，引用失效就直接报 stale、不派发点击。所以这时
    可以放心重来一轮；但**绝不能**在这里退化成回车——那有可能变成第二次提交。
    """

    class _El:
        def __init__(self):
            self.entered = False

        def click(self):
            raise StaleElementReferenceException("stale")

        def send_keys(self, keys):
            self.entered = True

    submit_el, password_el = _El(), _El()
    fields = SimpleNamespace(submit=submit_el, password=password_el)

    with pytest.raises(StaleElementReferenceException):
        browser_login._submit(_StubDriver(), fields)

    assert password_el.entered is False


def test_submit_falls_back_to_enter_for_a_non_stale_click_failure():
    """非 stale 的点击失败（比如按钮被别的东西挡住）仍然退回回车。"""
    from selenium.common.exceptions import ElementClickInterceptedException

    class _El:
        def __init__(self):
            self.entered = False

        def click(self):
            raise ElementClickInterceptedException("intercepted")

        def send_keys(self, keys):
            self.entered = True

    submit_el, password_el = _El(), _El()
    fields = SimpleNamespace(submit=submit_el, password=password_el)

    browser_login._submit(_StubDriver(), fields)

    assert password_el.entered is True


def test_attempt_gives_up_cleanly_when_the_page_never_stops_re_rendering(monkeypatch, caplog):
    """页面一直重渲染时要干净地收场：给出可读的原因，而不是抛出去。

    真机那种情况，用户看到的是 state.json 里一句 browser_error + 一条能看懂的
    日志，而不是一个栈回溯。
    """
    monkeypatch.setattr(browser_login, "_SETTLE_STEP", 0)
    monkeypatch.setattr(browser_login, "_STALE_RETRIES", 1)
    driver = _StubDriver(found=browser_login._PageChanged("一直重渲染"))
    cfg = SimpleNamespace(selectors={}, username="u", probe_timeout=1)

    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        result, image_el, reload_el = browser_login._attempt(driver, cfg, "pw", False, 1)

    assert result.ok is False
    assert result.reason == "browser_error"
    assert "重渲染" in result.detail or "重渲染" in caplog.text
    assert (image_el, reload_el) == (None, None)


def test_stale_retry_does_not_consume_a_captcha_attempt(monkeypatch, caplog):
    """重渲染的重试必须是"提交前重来"，不能占用 captcha_max_retry 的次数。

    锁号预算就靠这个：重渲染什么都没提交，不该算一次尝试。
    """
    monkeypatch.setattr(browser_login, "_SETTLE_STEP", 0)
    monkeypatch.setattr(browser_login, "_STALE_RETRIES", 2)
    driver = _StubDriver(found=browser_login._PageChanged("一直重渲染"))
    cfg = SimpleNamespace(selectors={}, username="u", probe_timeout=1)

    with caplog.at_level(logging.WARNING, logger="src.browser_login"):
        browser_login._attempt(driver, cfg, "pw", False, 1)

    # 重试次数由 _STALE_RETRIES 决定（=2 表示最多 3 轮），与验证码次数无关
    assert caplog.text.count("重新采集后再来一次") == 2
    assert "不计入验证码重试次数" in caplog.text


# --- 失败现场转储（密码绝不落盘）--------------------------------------------


def test_scrub_removes_the_password_in_every_encoding():
    source = ('<input type="password" value="p&amp;ss">'
              '<script>var p = "p&ss";</script>'
              '<a href="/x?p=p%26ss">next</a>')

    scrubbed = browser_login._scrub(source, "p&ss")

    assert "p&ss" not in scrubbed
    assert "p&amp;ss" not in scrubbed
    assert "p%26ss" not in scrubbed
    assert browser_login._MASK in scrubbed
    assert browser_login._contains_secret(scrubbed, "p&ss") is False


def test_contains_secret_checks_all_the_forms():
    assert browser_login._contains_secret('value="p&amp;ss"', "p&ss") is True
    assert browser_login._contains_secret("nothing here", "p&ss") is False


def test_dump_failure_clears_the_password_field_before_capturing(tmp_path):
    """截图会把输入框里的值直接画出来（密码框通常画圆点，但这个不保证），
    所以转储之前必须先把值清掉。"""
    box = _StubElement("密码框")
    driver = _StubDumpDriver(source="<html>ok</html>", password_fields=[box])
    cfg = SimpleNamespace(base_dir=str(tmp_path))

    browser_login._dump_failure(driver, cfg, "Sup3rSecret", "captcha_failed")

    assert box.cleared is True


def test_dump_failure_writes_source_and_screenshot_without_the_password(tmp_path):
    source = ('<html><input type="password" value="Sup3rSecret">'
              '<p>验证码错误，请重新输入</p></html>')
    driver = _StubDumpDriver(source=source)
    cfg = SimpleNamespace(base_dir=str(tmp_path))

    note = browser_login._dump_failure(driver, cfg, "Sup3rSecret", "captcha_failed")

    html_file = next(tmp_path.glob("failure-*.html"))
    text = html_file.read_text(encoding="utf-8")

    assert "Sup3rSecret" not in text                  # 密码绝不落盘
    assert browser_login._MASK in text
    assert "验证码错误，请重新输入" in text            # 有用的内容要留着
    assert html_file.with_suffix(".png").exists()     # 截图也落盘了
    assert str(html_file) in note


def test_dump_failure_refuses_to_write_html_when_the_password_cannot_be_scrubbed(
        tmp_path, monkeypatch):
    """抹不干净就宁可不写这个文件：失败诊断绝不能变成密码泄露的口子。

    这里把 _scrub 换成"什么都不抹"来模拟一个抹不掉的秘密（比如某种没预料到的
    编码形式），断言 html 没有被写出去。
    """
    monkeypatch.setattr(browser_login, "_scrub", lambda text, secret: text)
    driver = _StubDumpDriver(source='<input value="Sup3rSecret">')
    cfg = SimpleNamespace(base_dir=str(tmp_path))

    note = browser_login._dump_failure(driver, cfg, "Sup3rSecret", "captcha_failed")

    assert list(tmp_path.glob("failure-*.html")) == []      # html 宁可不写
    assert len(list(tmp_path.glob("failure-*.png"))) == 1   # 截图仍保存（值已清空）
    assert "未保存" in note


def test_dump_failure_survives_a_driver_that_cannot_read_anything(tmp_path):
    """转储本身是最后一道诊断手段，它自己出问题也不能把登录流程弄崩。"""

    class _BrokenDriver:
        page_source = ""

        def find_elements(self, by, value):
            raise WebDriverException("no window")

        def save_screenshot(self, path):
            raise WebDriverException("no window")

    cfg = SimpleNamespace(base_dir=str(tmp_path))

    note = browser_login._dump_failure(_BrokenDriver(), cfg, "pw", "browser_error")

    assert "失败现场" in note
