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
    is_displayed。用身份比较来断言"点的是哪一个"，所以不需要更多行为。
    """

    def __init__(self, name, ancestor=None, selected=False, displayed=True):
        self.name = name
        self._ancestor = ancestor
        self._selected = selected
        self._displayed = displayed

    def find_element(self, by, value):
        if self._ancestor is None:
            raise NoSuchElementException(value)
        return self._ancestor

    def is_selected(self):
        return self._selected

    def is_displayed(self):
        return self._displayed

    def __repr__(self):
        return f"<{self.name}>"


class _StubDriver:
    """假 driver：记录 JS 点击了哪个元素、整页刷新了几次，并按配置回答 find_elements。"""

    def __init__(self, found=None, js_ok=True):
        self.js_clicks = []
        self.refreshes = 0
        self._found = [] if found is None else found
        self._js_ok = js_ok

    def find_elements(self, by, value):
        if isinstance(self._found, Exception):
            raise self._found
        return list(self._found)

    def execute_script(self, script, el):
        if not self._js_ok:
            raise WebDriverException("blocked")
        self.js_clicks.append(el)

    def refresh(self):
        self.refreshes += 1


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
