"""browser_login 的纯函数层。

这里一个浏览器都不起：字段判定全部由 pick_* / locate 这几个纯函数负责，
描述符就是普通 dict，手搓出各种门户页面即可。

这个切分是刻意的——门户改版时最先坏的就是这一层，而它恰好不需要浏览器就能测。
"""
import logging
import time
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import NoSuchElementException, WebDriverException

from src import browser_login
from src.browser_login import (
    REASONS,
    _describe,
    decide_submit,
    judge,
    locate,
    pick_captcha_image,
    pick_captcha_input,
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
        "placeholder": "", "value": "", "src": "", "text": "", "visible": True,
        "size": (0, 0),
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

    class _Driver:
        def find_element(self, by, value):
            if value == "#user":
                return object()
            raise NoSuchElementException(f"no such element: {value}")

    text = render_overrides(_Driver(), {"username": "#user", "password": "#nope", "submit": ""})

    assert "#user → 命中" in text
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


def test_wait_for_password_field_returns_as_soon_as_the_form_appears():
    """表单已经在页面上时，这一步只该花一次查询的代价，不能白等。"""

    class _El:
        def is_displayed(self):
            return True

    class _Driver:
        def __init__(self):
            self.queries = 0

        def find_elements(self, by, value):
            self.queries += 1
            return [_El()]

    driver = _Driver()
    started = time.monotonic()
    browser_login._wait_for_password_field(
        driver, SimpleNamespace(selectors={}), time.monotonic() + 10)

    assert driver.queries == 1
    assert time.monotonic() - started < 1


def test_wait_for_password_field_gives_up_at_the_deadline():
    """页面上压根没有密码框时必须按时放弃。

    脚本是计划任务每 30 分钟拉起的短命进程，一个不肯超时的等待会把上一轮的进程
    一直留在那里。"""

    class _Driver:
        def find_elements(self, by, value):
            return []

    started = time.monotonic()
    browser_login._wait_for_password_field(
        _Driver(), SimpleNamespace(selectors={}), time.monotonic() + 1)
    elapsed = time.monotonic() - started

    assert 1 <= elapsed < 5


def test_wait_for_password_field_is_skipped_when_the_user_set_a_selector():
    """用户指定了 password 选择器就不用猜页面长什么样，也不该白等。"""

    class _Driver:
        def find_elements(self, by, value):
            raise AssertionError("指定了 password 选择器时不该去猜")

    browser_login._wait_for_password_field(
        _Driver(), SimpleNamespace(selectors={"password": "#pwd"}), time.monotonic() + 10)


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
