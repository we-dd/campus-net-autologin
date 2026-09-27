"""用浏览器自动化完成校园网认证。

思路：不复刻门户的 HTTP 登录（抓包没拿到、表单字段名未知、密码可能在前端加密、
URL 参数还是动态的），而是用 Selenium 驱动用户机器上装好的 Edge/Chrome，
像人一样打开登录页、填表、点提交。上面那几个问题由浏览器自己处理掉。

代价是拿不到响应体：登录成没成只能看"页面有没有从登录表单上离开"（见 judge）。
这个判据不完美，但它是浏览器方案下能做到的最好程度。

模块刻意分成两层：

  - 纯函数层（pick_* / locate / judge / render_elements）：只吃普通 dict 描述符，
    不碰浏览器，可以直接单元测试。门户改版时最先坏的正是这一层，而它恰好不需要
    浏览器就能测——这是这个文件里最重要的结构决定。
  - 驱动层（_describe / _build_driver / _attempt）：唯一接触 WebElement 的地方。

两条硬性约束：
  1. 密码的值绝不进日志（WebDriver 异常消息会把 send_keys 的内容带出来，
     所以密码输入失败时要换一个不带内容的异常，见 _type_password）。
  2. 任何异常都要变成 Result，不能有栈回溯冒出去——脚本由计划任务拉起，
     stderr 是丢掉的，一个未捕获的异常等于什么都没发生。
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.options import Options as EdgeOptions

from src import captcha, secret
from src.config import Config

log = logging.getLogger(__name__)

# 一次尝试里，点完提交后等页面反应的秒数。等的太短会把"还在提交中"当成失败。
_POST_SUBMIT_WAIT = 2.0
# 点完验证码图片（或刷新页面）后等新图加载出来的秒数。
_REFRESH_WAIT = 0.5
# 等登录表单出现时的轮询间隔（见 _wait_for_password_field）。
_FORM_WAIT_STEP = 0.5

# 表单元素的采集范围。一次 find_elements 用逗号选择器拿到的就是文档顺序，
# 描述符里的 index 因此可以跨标签比较大小——"离验证码图片最近"这类判断靠它。
_FORM_SELECTOR = "input, button, a, img"

# Result.reason 的全部取值。
# 前五个来自任务书；后两个是项目里既有的语义：docs 里 portal 的设计和
# state 的锁号窗口约定都要求把它们单独分开，不能混进 captcha_failed——
# 混进去的话，一次"密码错"会被当成"验证码没认出来"反复重试，正是锁号的主因。
REASONS = (
    "success",          # 认证成功
    "captcha_failed",   # 验证码错，或新验证码换了一张，可以再试
    "not_login_page",   # 找不到登录表单；或验证码只找到一半，需要人工校准
    "timeout",          # 打开页面超时，且页面上也没有登录表单
    "browser_error",    # 浏览器/驱动本身的故障
    "bad_credentials",  # 页面明确提示账号或密码错：确定性失败，重试只会加速锁号
    "secret",           # 本地密码文件读不出来，根本没发出登录请求
)


@dataclass
class Result:
    ok: bool
    reason: str
    detail: str = ""


class _PasswordSendError(Exception):
    """密码输入失败。

    单独一个异常类型不是为了分类，而是为了安全：WebDriverException 的消息里会把
    send_keys 的内容原样带出来（例如 "element not interactable: ... value=MyPass"），
    一旦让它带着密码往上冒，login() 的兜底 except 就会连密码一起写进日志文件。
    换成这个类型后，往上传递的只有 WebDriver 的异常类名。
    """


# ---------------------------------------------------------------------------
# 纯函数层：描述符 → 该用哪个元素
#
# 描述符就是一个普通 dict，字段见 _describe()。测试里手搓这些 dict 即可覆盖
# 各种门户页面，完全不需要起浏览器。
# ---------------------------------------------------------------------------

# input 不带 type 属性时浏览器按 text 处理，所以空串要当成 text。
_TEXTISH_TYPES = ("text", "tel", "email", "")

# 验证码图片尺寸上限。验证码图一律很小，超过这个尺寸的基本都是 logo。
# 宁可漏掉（用户可以靠 selectors.captcha_image 兜底），也不要错认一张 logo。
_CAPTCHA_MAX_W = 200
_CAPTCHA_MAX_H = 100

# 验证码图片的命名特征。大小写不敏感，同时看 src / id / name / class / placeholder。
# 分强/弱两档是必需的，不是洁癖：实测真实门户页面上同时有一个 14x14 的
# "显示/隐藏密码"眼睛图标和一个 80x28 的真验证码，两者都小到能过尺寸门槛，
# 而眼睛图标离密码框更近。只按距离或只按"有没有关键字"都可能挑错，分档之后
# 真验证码（src 含 captcha）落在强档，眼睛图标（class="icon-password-hide"）
# 一个词都不沾，强档直接胜出，完全不依赖距离。
_CAPTCHA_WORDS_STRONG = ("captcha", "validcode", "checkcode", "authcode", "yzm", "验证码")
# 弱档：锐捷等国产门户的图片就叫 code，弱档只在没有强档可选时才起作用。
_CAPTCHA_WORDS_WEAK = ("code", "verify", "vcode", "验证")

# "同意用户协议"勾选框附近的文字特征。
_AGREE_WORDS = ("同意", "协议", "agree", "accept", "terms")

# "换一张验证码"控件的文字特征，大小写不敏感。
_RELOAD_WORDS = ("换一个", "换一张", "刷新", "看不清", "reload", "refresh", "change")

# 提交按钮的文字特征，大小写不敏感。
_SUBMIT_TEXTS = ("登录", "登入", "连接", "login", "log in", "sign in", "signin")

# 页面文字里出现这些，说明账号或密码不对。
# 只放明确的整句，不放 "密码" 这种词——登录页自己就写着"密码"。
_CREDENTIAL_ERRORS = (
    "密码错误", "密码不正确", "密码有误",
    "用户名或密码错误", "用户名或密码不正确",
    "账号或密码错误", "帐号或密码错误", "账号或密码不正确",
    "用户不存在", "用户名不存在", "账号不存在", "帐号不存在",
    "认证失败", "登录失败",
    "invalid password", "incorrect password", "wrong password",
    "invalid username or password", "authentication failed",
)

# 页面文字里出现这些，说明验证码不对，换一张可以再试。
_CAPTCHA_ERRORS = (
    "验证码错误", "验证码不正确", "验证码有误", "验证码无效",
    "验证码已过期", "验证码失效",
    "invalid captcha", "invalid verification code", "captcha is incorrect",
)


def _type_of(desc: dict) -> str:
    """描述符的 type，取不到按"没有 type"处理。

    用 .get 而不是下标是有实测原因的：有的前端框架不给 input 写字面的 type 属性，
    这种描述符里可能压根没有 type 这个键，缺了要按"没有 type"（浏览器按 text 处理）
    来算，而不是抛 KeyError——真实门户页面的用户名框就是这种情况。
    纯函数层一律用这个取值，别处不要直接下标 desc["type"]。
    """
    return desc.get("type", "")


def _is_textish(desc: dict) -> bool:
    """是不是"可以当文本框用"的可见 input。"""
    return (
        desc["tag"] == "input"
        and desc["visible"]
        and _type_of(desc) in _TEXTISH_TYPES
    )


def pick_username(controls: list[dict]) -> dict | None:
    """用户名框：第一个可见的文本类输入框（text / tel / email / 无 type）。"""
    for desc in controls:
        if _is_textish(desc):
            return desc
    return None


def pick_password(controls: list[dict]) -> dict | None:
    """密码框：第一个可见的 password 输入框。

    找不到它基本就说明这不是登录页（普通页面上不会有密码框），
    所以这个函数的结果是"我们在不在登录页"的主要判据。
    """
    for desc in controls:
        if desc["tag"] == "input" and desc["visible"] and _type_of(desc) == "password":
            return desc
    return None


def _captcha_rank(desc: dict) -> int:
    """命名像不像验证码：0 很像，1 有点像，2 看不出来。"""
    haystack = " ".join(
        (desc["src"], desc["id"], desc["name"], desc["class"], desc["placeholder"])
    ).lower()
    if any(word in haystack for word in _CAPTCHA_WORDS_STRONG):
        return 0
    if any(word in haystack for word in _CAPTCHA_WORDS_WEAK):
        return 1
    return 2


def _is_captcha_sized(desc: dict) -> bool:
    """尺寸像不像验证码图。

    宽或高为 0 的图片一并排除：那是没加载出来 / 被 CSS 藏起来的图，
    screenshot_as_png 抓到的会是空白。
    """
    width, height = desc["size"]
    return 0 < width <= _CAPTCHA_MAX_W and 0 < height <= _CAPTCHA_MAX_H


def pick_captcha_image(images: list[dict], anchor: dict | None) -> dict | None:
    """挑出验证码图片。

    规则分两步，先卡尺寸再看名字：
      1. 尺寸必须像验证码（小图）。这一步是硬门槛：页面上的大图基本都是 logo，
         把 logo 当验证码抓去识别，识别结果必然是垃圾，然后就是一串白费的登录尝试。
         （注意小图里也会有干扰项，比如密码框的"显示密码"眼睛图标，所以还要看第 2 步。）
      2. 合格的小图里按命名分档：强档（captcha/validcode/…）优先，其次弱档
         （code/verify/…），最后才是完全看不出名字的，档内再取离 anchor（密码框）
         最近的。实测的真验证码 src 含 captcha，会稳稳落在强档。
    """
    candidates = [d for d in images if d["visible"] and _is_captcha_sized(d)]
    if not candidates:
        return None

    # 覆盖方式进来的元素没有文档顺序（index 为 -1），此时不做距离比较。
    anchor_index = anchor["index"] if anchor and anchor["index"] >= 0 else None

    def key(desc: dict) -> tuple:
        distance = abs(desc["index"] - anchor_index) if anchor_index is not None else 0
        # 名字像不像排第一优先级，其次距离近的，最后文档顺序靠前的（结果才稳定）
        return (_captcha_rank(desc), distance, desc["index"])

    return min(candidates, key=key)


def pick_agree(controls: list[dict]) -> dict | None:
    """挑出"同意用户协议"的勾选框。

    刻意**不**按可见性过滤：实测页面上 ant-design 把真实 input 藏了起来
    （is_displayed() 为 False），可见的只是它外面的样式块，按可见性筛等于永远
    找不到。优先取附近文字提到 同意/协议 的那个，取不到就退回第一个复选框。

    找不到就返回 None，调用方跳过勾选、继续提交（这个页面实测按钮一直是可用的，
    勾选框不是硬门槛）。
    """
    boxes = [d for d in controls if d["tag"] == "input" and _type_of(d) == "checkbox"]
    if not boxes:
        return None

    def key(desc: dict) -> tuple:
        nearby = desc.get("nearby", "").lower()
        return (0 if any(word in nearby for word in _AGREE_WORDS) else 1, desc["index"])

    return min(boxes, key=key)


def pick_captcha_reload(controls: list[dict], image: dict | None) -> dict | None:
    """挑出"换一张验证码"的控件。

    实测：直接点验证码图片会被拦下来（ElementClickInterceptedException）——真正
    接收点击的是包着它的 <a class="code-reload-content">（文字"换一个"）。所以先把
    这个控件找出来；找不到就返回 None，调用方会依次退回图片的可点击祖先、图片本身。

    只认可见的 <a>/<button>，多个候选时取离验证码图片最近的。
    """
    if image is None or image["index"] < 0:
        return None

    candidates = [
        d for d in controls
        if d["tag"] in ("a", "button") and d["visible"]
        and any(word in _label(d) for word in _RELOAD_WORDS)
    ]
    if not candidates:
        return None

    return min(candidates, key=lambda d: (abs(d["index"] - image["index"]), d["index"]))


def pick_captcha_input(controls: list[dict], image: dict | None, taken: set[int]) -> dict | None:
    """挑出验证码输入框：离验证码图片最近的可见文本输入框。

    taken 是已经被用户名、密码占掉的下标，必须排除。验证码图片放在用户名左边时
    （确实有这样的门户），"离图片最近的文本框"就是用户名框，不排除的话验证码会
    被打进学号里，然后每次都因为验证码为空而失败。
    """
    if image is None or image["index"] < 0:
        # 没有验证码图片就无从判断哪个输入框是验证码框（页面上可能有两三个文本框），
        # 宁可放弃也不要瞎猜。
        return None

    candidates = [d for d in controls if _is_textish(d) and d["index"] not in taken]
    if not candidates:
        return None

    return min(candidates, key=lambda d: (abs(d["index"] - image["index"]), d["index"]))


def _label(desc: dict) -> str:
    """控件的可见文字，转小写。

    input 型按钮的文字在 value 属性里，不在 .text 里（.text 对 input 恒为空），
    所以两个都要看。
    """
    return f'{desc["text"]} {desc["value"]}'.strip().lower()


def pick_submit(controls: list[dict]) -> dict | None:
    """挑出提交控件。

    优先级从高到低，同一优先级里取文档顺序最靠前的：
      1. 显式提交控件：input[type=submit]、button[type=submit] 或没有 type 的 button
         （button 不写 type 时浏览器默认就是 submit）；
      2. 其他可见 button（有些门户用 button[type=button] 加 onclick 提交）；
      3. 文字里带"登录/login"的可见元素（用 <a> 或 <div> 当按钮的门户）。
    找不到就返回 None，调用方用回车兜底。
    """
    visible = [d for d in controls if d["visible"]]

    explicit = [
        d for d in visible
        if (d["tag"] == "input" and _type_of(d) == "submit")
        or (d["tag"] == "button" and _type_of(d) in ("", "submit"))
    ]
    if explicit:
        return min(explicit, key=lambda d: d["index"])

    buttons = [d for d in visible if d["tag"] == "button"]
    if buttons:
        return min(buttons, key=lambda d: d["index"])

    by_text = [d for d in visible if any(w in _label(d) for w in _SUBMIT_TEXTS)]
    if by_text:
        return min(by_text, key=lambda d: d["index"])

    return None


def _brief(controls: list[dict], images: list[dict]) -> str:
    """一句话概括页面上有什么，用来解释"为什么判定不是登录页"。"""
    everything = controls + images

    def count(tag: str) -> int:
        return sum(1 for d in everything if d["tag"] == tag)

    visible_inputs = [d for d in controls if d["tag"] == "input" and d["visible"]]
    types = "、".join(sorted({(_type_of(d) or "无 type") for d in visible_inputs})) or "无"
    return (
        f"页面上共 {len(everything)} 个表单元素"
        f"（input {count('input')} / button {count('button')} / "
        f"a {count('a')} / img {count('img')}），"
        f"其中可见输入框 {len(visible_inputs)} 个，type 是 {types}"
    )


def _missing_detail(controls, images, has_user: bool, has_pwd: bool) -> str:
    """解释为什么判定"不是登录页"。给用户看的，所以要带上页面上到底有什么。"""
    missing = []
    if not has_user:
        missing.append("用户名输入框")
    if not has_pwd:
        missing.append("密码输入框")
    return (
        f"没找到{'、'.join(missing)}，当前页面不像是登录页。"
        f"{_brief(controls, images)}。"
        f"可以运行 python login.py --dump-form 看完整清单"
    )


@dataclass
class Page:
    """一次页面解析的结果。

    全是描述符（普通 dict）或 None，不带 WebElement，所以整个解析过程都能在没有
    浏览器的环境下测试。reason 非空表示这次解析不能用，原因写在 detail 里。
    """

    username: dict | None = None
    password: dict | None = None
    captcha_image: dict | None = None
    captcha_input: dict | None = None
    submit: dict | None = None
    agree: dict | None = None
    captcha_reload: dict | None = None
    reason: str = ""
    detail: str = ""


def locate(controls: list[dict], images: list[dict]) -> Page:
    """决定页面上哪个元素是哪个字段。

    这是整个模块里最需要测的一段：门户一改版它就坏，而它又不需要浏览器。
    """
    user = pick_username(controls)
    pwd = pick_password(controls)
    if user is None or pwd is None:
        return Page(
            user, pwd, reason="not_login_page",
            detail=_missing_detail(controls, images, user is not None, pwd is not None),
        )

    image = pick_captcha_image(images, pwd)
    cap = pick_captcha_input(controls, image, {user["index"], pwd["index"]})

    if (image is None) != (cap is None):
        # 一有一没有，说明页面确实有验证码，是我们的启发式没对上。
        # 用同样的找法重试只会重复同样的失败，而每次失败都在消耗锁号预算，
        # 所以直接放弃这次运行，让用户拿 --dump-form 的输出校准 selectors。
        found = "图片" if image is not None else "输入框"
        missing = "输入框" if image is not None else "图片"
        return Page(
            user, pwd, image, cap, reason="not_login_page",
            detail=(
                f"页面上似乎有验证码，但只找到了验证码{found}，没找到验证码{missing}。"
                f"请运行 python login.py --dump-form 看页面元素，"
                f"再用 config.json 的 selectors 手动指定"
            ),
        )

    return Page(
        user, pwd, image, cap, pick_submit(controls),
        agree=pick_agree(controls),
        captcha_reload=pick_captcha_reload(controls, image),
    )


def decide_submit(needs_captcha: bool, captcha_text: str) -> tuple[bool, str]:
    """要不要真的点提交，返回 (是否提交, 说明)。

    抽成纯函数是因为这是防锁号的一环：页面需要验证码、而我们没识别出来时，
    这一票投出去是必然失败的，但它会真的打到认证服务器上、消耗一次锁号预算。
    宁可不提交，留到下一轮换张验证码再试——重试上限本来就是这个用途。

    （页面不需要验证码时不受影响：那种页面根本不会走到这里。）
    """
    if needs_captcha and not captcha_text:
        return False, "验证码识别结果为空，本次没有提交（省下一次锁号预算）"
    return True, ""


def judge(snapshot: dict) -> tuple[bool, str, str]:
    """判断这次提交成没成，返回 (是否成功, reason, 说明)。

    浏览器方案下没有响应体可解析，只能看页面本身变了没有：
      1. 先找明确的错误提示（页面的可见文字）。它比"表单还在不在"可靠，
         因为错误提示能顺便告诉我们该不该继续重试；
      2. 再看密码框还在不在：密码框消失算成功。

    第 2 步刻意只看密码框，不看用户名框：登录成功后门户常常跳回学校主页，
    那上面是有搜索框的。把那种页面判成"表单还在"，就会继续提交，而那些提交
    会真的打到认证服务器上、消耗锁号预算。反过来判成成功的代价小得多——
    下一次 --check（30 分钟后）重新探测，发现还是没网就再登一次，不消耗预算。
    所以两边都可能判错时，选便宜的那一边错。

    局限（刻意保持简单，不在这一版里做）：
      - 门户若在失败时跳到一个既没有错误字样、也没有密码框的页面，
        这里会误判成成功（代价见上，可接受）；
      - 只认中文和英文的提示语，其它语言的界面识别不了。
    """
    text = (snapshot.get("body_text") or "").lower()

    for phrase in _CREDENTIAL_ERRORS:
        if phrase.lower() in text:
            return False, "bad_credentials", f"页面提示「{phrase}」"

    for phrase in _CAPTCHA_ERRORS:
        if phrase.lower() in text:
            return False, "captcha_failed", f"页面提示「{phrase}」"

    if not snapshot.get("has_password"):
        extra = "，页面上仍有其它输入框" if snapshot.get("has_username") else ""
        return True, "success", f"密码框已从页面上消失{extra}"

    return False, "captcha_failed", "登录表单仍在页面上"


def render_elements(descriptors: list[dict]) -> str:
    """把描述符列表渲染成给人看的多行文本（--dump-form 用）。"""
    if not descriptors:
        return "页面上没有找到 input / button / a / img 元素。"

    lines = []
    for d in descriptors:
        width, height = d["size"]
        lines.append(
            f'[{d["index"]:>3}] <{d["tag"]}> type={_type_of(d) or "-"} '
            f'id={d["id"] or "-"} name={d["name"] or "-"} class={d["class"] or "-"} '
            f'placeholder={d["placeholder"] or "-"} '
            f'可见={"是" if d["visible"] else "否"} 尺寸={width}x{height} '
            f'value={d["value"] or "-"} 文字={d["text"] or "-"}'
        )
    return "\n".join(lines)


def _suggest(desc: dict | None) -> str:
    """给一个元素提一个能直接写进 selectors 的 CSS 选择器。"""
    if desc is None:
        return "没找到"
    if desc["id"]:
        return f'#{desc["id"]}'
    if desc["name"]:
        return f'{desc["tag"]}[name="{desc["name"]}"]'
    return f'（第 {desc["index"]} 个元素，没有 id/name，建议按 F12 自行复制选择器）'


def render_verdict(controls: list[dict], images: list[dict]) -> str:
    """把启发式的判定结果渲染出来，用户才知道该覆盖哪个 selectors。"""
    page = locate(controls, images)
    lines = [
        f"用户名框   : {_suggest(page.username)}",
        f"密码框     : {_suggest(page.password)}",
        f"验证码图片 : {_suggest(page.captcha_image)}",
        f"验证码输入 : {_suggest(page.captcha_input)}",
        f"验证码刷新 : {_suggest(page.captcha_reload)}",
        f"同意勾选框 : {_suggest(page.agree)}",
        f"提交按钮   : {_suggest(page.submit)}",
    ]
    if page.reason:
        lines.append("")
        lines.append(f"自动识别不完整（{page.reason}）：{page.detail}")
        lines.append(
            '把上面某个元素的选择器填进 config.json 的 selectors 即可，'
            '例如："selectors": {"captcha_image": "#validcodeImg"}'
        )
    return "\n".join(lines)


def render_overrides(driver, selectors: dict) -> str:
    """渲染 config.json 里配置的 selectors 在页面上到底命中没有。

    用户配了一个选择器之后，唯一的确认办法就是再跑一次 --dump-form；
    没有这一段，他没法知道那个选择器是有用还是写错了（写错了只会静默退回启发式）。
    """
    if not selectors:
        return "（没有配置 selectors，全部使用自动识别）"

    lines = []
    for key, value in selectors.items():
        if not value:
            lines.append(f"{key}: 留空 → 使用自动识别")
            continue
        # 用和正式流程同一个查找函数，这样"命中"的含义完全一致
        # （包括"多个匹配时优先取可见的那个"）。
        el = _find_by_selector(driver, value, key)
        lines.append(
            f"{key}: {value} → {'命中' if el is not None else '没找到，会退回自动识别'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 驱动层：唯一接触 selenium 的地方
# ---------------------------------------------------------------------------


def _attr(el, name: str) -> str:
    """读属性。读不到返回空串：元素在采集过程中失效时不该炸掉整轮尝试。"""
    try:
        return el.get_attribute(name) or ""
    except WebDriverException as e:
        log.debug("读取属性 %s 失败: %s", name, e)
        return ""


def _visible(el) -> bool:
    try:
        return bool(el.is_displayed())
    except WebDriverException:
        return False


def _text(el) -> str:
    """读可见文字。

    对不可见的元素 .text 会走一次 JS 往返，个别情况下还会抛，所以必须兜住——
    采集一个元素失败不该让整轮尝试崩掉。
    """
    try:
        return (el.text or "").strip()
    except WebDriverException as e:
        log.debug("读取元素文字失败: %s", e)
        return ""


def _nearby_text(el) -> str:
    """元素附近的文字，最多往上找两层。

    复选框自己是没有文字的（input 的 text 恒为空），它的说明文字在父节点或兄弟
    节点里——实测页面上的"请先阅读并同意《用户协议》…"就摆在勾选框旁边的节点里。
    两层还取不到就返回空串，调用方会退回"第一个复选框"的兜底逻辑。
    """
    node = el
    for _ in range(2):
        try:
            node = node.find_element(By.XPATH, "..")
            text = (node.text or "").strip()
        except WebDriverException as e:
            log.debug("读取元素附近文字失败: %s", e)
            return ""
        if text:
            return text
    return ""


def _describe(el) -> dict:
    """把一个 WebElement 压成纯数据描述符。

    index 先给 -1，由 _collect 按文档顺序覆盖。单独列出来是因为描述符要能脱离
    浏览器构造——测试里的页面就是这么手搓出来的。
    """
    try:
        size = el.size or {}
    except WebDriverException:
        size = {}

    kind = _attr(el, "type").strip().lower()

    return {
        "index": -1,
        "tag": (el.tag_name or "").lower(),
        "type": kind,
        "id": _attr(el, "id"),
        "name": _attr(el, "name"),
        "class": _attr(el, "class"),
        "placeholder": _attr(el, "placeholder"),
        "value": _attr(el, "value"),
        "src": _attr(el, "src"),
        "text": _text(el),
        # 只有复选框需要：它的文字在旁边的节点里，其它元素用不上，省掉往返
        "nearby": _nearby_text(el) if kind == "checkbox" else "",
        "visible": _visible(el),
        "size": (int(size.get("width") or 0), int(size.get("height") or 0)),
    }


def _collect(driver) -> tuple[list, list[dict]]:
    """按文档顺序采集表单元素，返回 (elements, descriptors) 两个平行列表。

    一次 find_elements 用逗号选择器拿到的就是文档顺序，所以描述符里的 index 同时
    也是 elements 里的下标，"离谁最近"这类判断可以直接跨标签比。
    """
    elements = driver.find_elements(By.CSS_SELECTOR, _FORM_SELECTOR)
    descriptors = []
    for index, el in enumerate(elements):
        desc = _describe(el)
        desc["index"] = index
        descriptors.append(desc)
    return elements, descriptors


def _current_url(driver) -> str:
    try:
        return driver.current_url or ""
    except WebDriverException:
        return ""


def _silence_selenium_logging() -> None:
    """把 selenium 自己的日志压到 WARNING。

    这不是嫌它吵，是为了堵密码泄漏：selenium 的 remote_connection 在 DEBUG 级别
    会把每个 WebDriver 请求的完整请求体写进日志，而输入密码走的正是 send_keys，
    于是明文密码会出现在 campus-net.log 里。实测（--verbose 打开 DEBUG）：

        POST http://localhost:.../element/.../value
             {'text': 'Sup3rSecret!DoNotLog', 'value': ['S','u','p',...]}
        Remote response: status=200 | data={"value":"Sup3rSecret!DoNotLog"}

    用户排查"为什么登不上"时第一件事就是开 --verbose，所以这条路一定会被走到。
    本模块不许改 logsetup.py（那里的根 logger 级别是用户配的），所以在接触
    Selenium 之前就地关掉。

    直接关在父 logger "selenium" 上就够：子 logger 没单独设级别，会继承它，
    DEBUG 记录在创建阶段就被丢掉，根本不会传播到根 logger 的 handler。
    urllib3 / websocket 一并压掉，它们同样会把请求细节写出来。
    """
    for name in ("selenium", "urllib3", "websocket"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _build_driver(cfg: Config, profile: str):
    """启动浏览器。

    必须给一个临时配置目录：直接用默认目录会和用户日常开着的那个浏览器抢，
    实测报 "user data directory is already in use" 直接起不来；而且我们也不想
    把自动化留下的 cookie、缓存、首启状态写进用户的正常配置里。
    """
    # 必须在任何 WebDriver 请求之前关（见 _silence_selenium_logging）
    _silence_selenium_logging()

    if cfg.browser == "chrome":
        options = ChromeOptions()
        factory = webdriver.Chrome
    else:
        options = EdgeOptions()
        factory = webdriver.Edge

    options.add_argument(f"--user-data-dir={profile}")
    # 全新的配置目录会触发首启流程和"选择搜索引擎"页面，它们会盖住登录页，
    # 让后面的元素采集全部落空。
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-search-engine-choice-screen")
    if cfg.headless:
        options.add_argument("--headless=new")

    log.info("启动 %s 浏览器（临时配置目录 %s，headless=%s）",
             cfg.browser, profile, cfg.headless)

    driver = factory(options=options)
    driver.set_page_load_timeout(cfg.probe_timeout)
    return driver


def _quit(driver) -> None:
    """关掉浏览器。这里绝不能抛：异常会把已经拿到的判定结果吃掉。"""
    try:
        driver.quit()
    except Exception as e:
        log.warning("关闭浏览器失败（忽略）: %s", e)


def _navigate(driver, cfg: Config) -> bool:
    """打开登录页，返回这次导航是否超时。

    目标地址优先用配置里的 login_url；没配就用第一条探测地址，让门户把浏览器
    劫持到登录页去——这正是"不需要知道门户真实地址"的关键。
    """
    if cfg.login_url:
        target = cfg.login_url
    elif cfg.probe_urls:
        target = cfg.probe_urls[0]["url"]
    else:
        target = cfg.portal

    log.info("打开 %s", target)
    try:
        driver.get(target)
        return False
    except TimeoutException:
        # 门户劫持时经常把连接一直挂着不返回，但页面其实已经渲染出来了。
        # 所以这里只记一笔，不当失败：继续按当前页面状态往下走。
        log.warning("页面加载超时（门户劫持时很常见），按当前页面状态继续")
        return True


def _password_visible(driver) -> bool:
    """页面上有没有可见的密码框。

    先用字面的 input[type=password] 查：一次往返，实测真实门户命中。
    查不到再退回"取所有 input、按 type 属性逐个判断"——有的前端框架不给 input 写
    字面的 type 属性（实测本页的用户名框、验证码框都没有），那时 CSS 的 [type=...]
    是匹配不上的，只能靠 get_attribute 兜底。多花的往返只在第一种查不到时才付。
    """
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "input[type=password]"):
            if _visible(el):
                return True
    except WebDriverException as e:
        log.debug("查询密码框失败（页面可能还在跳转）: %s", e)

    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "input"):
            if _attr(el, "type").strip().lower() == "password" and _visible(el):
                return True
    except WebDriverException as e:
        log.debug("兜底查询密码框失败: %s", e)

    return False


def _wait_for_password_field(driver, cfg: Config, deadline: float) -> None:
    """等密码框出现，最多等到 deadline。

    这条等待是给门户劫持那条路准备的：driver.get() 在劫持场景下经常超时返回
    （门户把连接挂着不返回，见 _navigate），而那一刻页面很可能还在渲染——重定向链
    还没走完。只采一次的话，会把"还没画出来"错判成"不是登录页"，然后放弃整轮，
    而这一轮本来是可以成功的。

    用一条便宜的查询轮询，而不是反复 _collect()：_collect 会把每个
    input/button/a/img 的属性都读一遍（几十次 WebDriver 往返），半秒轮询一次
    太浪费。

    用户如果指定了 password 选择器就跳过等待——他既然知道页面长什么样，
    就按他给的来，不必猜。
    """
    if cfg.selectors.get("password"):
        return

    while True:
        if _password_visible(driver):
            return
        if time.monotonic() >= deadline:
            log.debug("等待密码框超时，按当前页面状态继续")
            return
        time.sleep(_FORM_WAIT_STEP)


def _fill(el, value: str) -> None:
    """清空并填入。clear() 失败不算致命，退回直接输入。"""
    try:
        el.clear()
    except WebDriverException as e:
        log.debug("清空输入框失败，直接输入: %s", e)
    el.send_keys(value)


def _type_password(el, value: str) -> None:
    """往密码框里输入。

    这个函数存在的唯一理由是安全：WebDriverException 的消息里会把 send_keys 的
    内容原样带出来（例如 "element not interactable: ... value=MyPass123"）。
    直接让它往上冒，密码就会顺着 login() 的兜底 except 进日志文件，而日志文件
    会在磁盘上长期留着。所以这里换成只带类名、不带内容的异常。
    """
    try:
        el.clear()
        el.send_keys(value)
    except WebDriverException as e:
        raise _PasswordSendError(type(e).__name__) from None


def _press_enter(el) -> None:
    """在密码框上按回车提交（页面上找不到提交按钮时的兜底）。

    同样要防泄漏，而且是同一条路径：send_keys 失败时 WebDriver 的异常消息里可能
    带着元素当前的值，而这个元素刚被填进密码。所以照 _type_password 的办法处理。
    """
    try:
        el.send_keys(Keys.ENTER)
    except WebDriverException as e:
        raise _PasswordSendError(type(e).__name__) from None


def _snapshot(driver) -> dict:
    """采集判定用的页面状态。每一步失败都只影响对应的字段，不整体失败。"""
    try:
        _elements, descriptors = _collect(driver)
    except WebDriverException as e:
        log.debug("采集页面元素失败: %s", e)
        descriptors = []

    controls = [d for d in descriptors if d["tag"] != "img"]

    body_text = ""
    try:
        # 只需要用来看有没有错误提示，截断是为了不在超大页面上白读几十万字符。
        body_text = (driver.find_element(By.TAG_NAME, "body").text or "")[:20000]
    except WebDriverException as e:
        log.debug("读取页面文字失败: %s", e)

    return {
        "url": _current_url(driver),
        "has_password": any(d["visible"] and _type_of(d) == "password" for d in controls),
        "has_username": any(_is_textish(d) for d in controls),
        "body_text": body_text,
    }


def _find_by_selector(driver, selector: str, kind: str):
    """按用户给的选择器找元素，多个匹配时优先返回第一个可见的。找不到返回 None。

    这条"优先可见"不是讲究，是实测出来的 bug：真实门户页面上
    input[name=captcha_code] 同时匹配一个隐藏 input（name 一样的隐藏字段）和一个
    可见的验证码输入框，而 find_element 返回的是文档顺序靠前的那个——隐藏的。
    往里打字等于没打，验证码框始终是空的，于是每次都提交一个空验证码。

    配的选择器匹配不到时返回 None，由调用方退回自动识别（失配只警告、不报错：
    用户在 config.json 里写错一个选择器，最坏的结果应该是回到自动识别，
    而不是整个工具登不上网）。

    顺带一个坑：有的前端框架不给 input 写字面的 type 属性（实测本页的用户名框和
    验证码框都没有），这时 CSS 的 [type=text] 匹配不上，只能靠 get_attribute 读。
    用户写 [type=...] 又没匹配到时，这里会多给一句提示。
    """
    try:
        found = driver.find_elements(By.CSS_SELECTOR, selector)
    except WebDriverException as e:
        log.warning("selectors.%s = %r 不是合法的选择器（%s），回退到自动识别",
                    kind, selector, type(e).__name__)
        return None

    if not found:
        hint = ""
        if "[type=" in selector:
            hint = ("；提示：有的输入框没有字面的 type 属性（本页用户名框、验证码框就是），"
                    "把 [type=...] 换成 #id 或 [name=...] 更稳")
        log.warning("selectors.%s = %r 在页面上没找到，回退到自动识别%s", kind, selector, hint)
        return None

    if len(found) == 1:
        return found[0]

    for position, el in enumerate(found, start=1):
        if _visible(el):
            log.info("selectors.%s = %r 匹配到 %d 个元素，选用第 %d 个（第一个可见的）",
                     kind, selector, len(found), position)
            return el

    log.info("selectors.%s = %r 匹配到 %d 个元素，但都不可见，选用第 1 个",
             kind, selector, len(found))
    return found[0]


def _resolve(driver, elements, override: str, desc: dict | None, kind: str):
    """定一个字段该用哪个元素：配了覆盖选择器就用它，否则用启发式的结论。

    覆盖项失配时只警告、不报错，退回启发式（见 _find_by_selector）。
    """
    if override:
        el = _find_by_selector(driver, override, kind)
        if el is not None:
            return el
    if desc is None:
        return None
    return elements[desc["index"]]


def _attempt(driver, cfg: Config, password: str, nav_timed_out: bool, attempt: int):
    """做一次完整的登录尝试。

    返回 (Result, 验证码图片元素, 验证码刷新元素)，后两个供下一轮刷新验证码用。
    """
    # 先等表单画出来再采（见 _wait_for_password_field）。表单已经在页面上时这一步
    # 只花一次查询的代价。
    _wait_for_password_field(driver, cfg, time.monotonic() + cfg.probe_timeout)

    elements, descriptors = _collect(driver)
    controls = [d for d in descriptors if d["tag"] != "img"]
    images = [d for d in descriptors if d["tag"] == "img"]
    selectors = cfg.selectors

    # 页面解析交给纯函数（纯函数层），这里只负责把结论映射回 WebElement。
    page = locate(controls, images)

    username_el = _resolve(driver, elements,
                           selectors.get("username", ""), page.username, "username")
    password_el = _resolve(driver, elements,
                           selectors.get("password", ""), page.password, "password")

    if username_el is None or password_el is None:
        detail = _missing_detail(controls, images,
                                 username_el is not None, password_el is not None)
        # 表单没找到：如果这次导航本来就超时了，那更该按超时报告——
        # 否则用户会去查"是不是登录页变了"，而真正的原因只是页面没加载完。
        reason = "timeout" if nav_timed_out else "not_login_page"
        log.error("第 %d 次尝试放弃: %s", attempt, detail)
        return Result(False, reason, detail), None, None

    # 剩下唯一一种 page.reason 是"验证码只找到一半"。用户把验证码的两个选择器都
    # 配好了就不适用这条——他的选择器会覆盖启发式，这时按配置继续。
    if page.reason and not (selectors.get("captcha_image") and selectors.get("captcha_input")):
        log.error("第 %d 次尝试放弃: %s", attempt, page.detail)
        return Result(False, page.reason, page.detail), None, None

    image_el = _resolve(driver, elements, selectors.get("captcha_image", ""),
                        page.captcha_image, "captcha_image")
    captcha_el = _resolve(driver, elements, selectors.get("captcha_input", ""),
                          page.captcha_input, "captcha_input")
    reload_el = _resolve(driver, elements, selectors.get("captcha_reload", ""),
                         page.captcha_reload, "captcha_reload")
    agree_el = _resolve(driver, elements, selectors.get("agree", ""),
                        page.agree, "agree")
    submit_el = _resolve(driver, elements, selectors.get("submit", ""),
                         page.submit, "submit")

    # 验证码必须在填密码之前抓：有些门户一输入密码就把验证码刷新掉。
    captcha_text = ""
    if image_el is not None:
        captcha_text = _solve_captcha(image_el)

    ok_to_submit, why_not = decide_submit(captcha_el is not None, captcha_text)
    if not ok_to_submit:
        log.warning("第 %d 次尝试：%s", attempt, why_not)
        return Result(False, "captcha_failed", why_not), image_el, reload_el

    _fill(username_el, cfg.username)
    _type_password(password_el, password)  # 注意：绝不记录 password 的值
    if captcha_el is not None:
        _fill(captcha_el, captcha_text)
    # 勾选放在填完之后、提交之前：这是真实登录的动作顺序，也避免勾选触发的
    # 重渲染把已经填好的内容弄乱。
    _tick_agree(driver, agree_el)

    if submit_el is None:
        log.info("没找到提交按钮，改用回车提交")
        _press_enter(password_el)
    else:
        try:
            submit_el.click()
        except WebDriverException as e:
            log.warning("点击提交按钮失败（%s），改用回车提交", type(e).__name__)
            _press_enter(password_el)

    time.sleep(_POST_SUBMIT_WAIT)
    snapshot = _snapshot(driver)
    ok, reason, detail = judge(snapshot)
    if ok:
        return Result(True, reason, detail), image_el, reload_el

    log.warning("第 %d 次尝试未成功: %s", attempt, detail)
    return Result(False, reason, detail), image_el, reload_el


def _js_click(driver, el) -> bool:
    """用 JS 点击元素，成功返回 True。

    这里不能用普通的 el.click()，实测两类元素都会失败：
      - ant-design 的"同意用户协议"复选框把真实 input 藏起来了
        （is_displayed() 为 False），普通 click 点不到；
      - 验证码图片会被包着它的 <a class="code-reload-content"> 拦掉，报
        ElementClickInterceptedException（浏览器认为点在那个 <a> 上）。
    JS 点击直接把事件派发到元素上，两种情况都能过——实测复选框的
    is_selected() 由 False 变成 True。
    """
    try:
        driver.execute_script("arguments[0].click()", el)
        return True
    except WebDriverException as e:
        log.debug("JS 点击失败: %s", e)
        return False


def _clickable_ancestor(driver, el):
    """离元素最近的可点击祖先（<a>）。没有就返回 None。"""
    try:
        return el.find_element(By.XPATH, "./ancestor::a[1]")
    except (NoSuchElementException, WebDriverException):
        return None


def _tick_agree(driver, el) -> None:
    """勾上"同意用户协议"。

    实测这个复选框并不控制提交按钮的可用状态（按钮一直是 enabled），所以它更像
    一道校验而不是硬门槛；但 ant-design 的表单校验仍可能拦住提交，而且勾上它才是
    真实登录时的动作，所以照勾。

    勾不上不算致命：只警告、继续提交，让后面的判定去说明结果。
    """
    if el is None:
        log.debug("页面上没找到「同意协议」勾选框，跳过")
        return

    try:
        if el.is_selected():
            log.info("用户协议勾选框已经是勾上的，不再点击")
            return
    except WebDriverException as e:
        log.debug("读取勾选框状态失败（继续尝试点击）: %s", e)

    if _js_click(driver, el):
        log.info("已勾选用户协议勾选框")
    else:
        log.warning("勾选用户协议失败，继续提交（门户可能会因此拒绝）")


def _refresh_captcha(driver, reload_el, image_el) -> None:
    """换一张验证码。

    顺序是有实测依据的：真正接收点击的是包着验证码图片的
    <a class="code-reload-content">（文字"换一个"），直接点图片会被它拦掉
    （ElementClickInterceptedException）。所以先点刷新控件，再退回点图片的
    可点击祖先，再退回点图片本身，最后才是整页刷新——整页刷新会把已经填好的
    内容清掉，所以放最后。
    """
    if reload_el is not None and _js_click(driver, reload_el):
        time.sleep(_REFRESH_WAIT)
        log.debug("已点击验证码刷新控件")
        return

    if image_el is not None:
        ancestor = _clickable_ancestor(driver, image_el)
        if ancestor is not None and _js_click(driver, ancestor):
            time.sleep(_REFRESH_WAIT)
            log.debug("已点击验证码图片外层的可点击祖先")
            return
        if _js_click(driver, image_el):
            time.sleep(_REFRESH_WAIT)
            log.debug("已点击验证码图片")
            return

    try:
        driver.refresh()
        time.sleep(_REFRESH_WAIT)
    except WebDriverException as e:
        log.debug("刷新页面失败（继续尝试）: %s", e)


def _solve_captcha(image_el) -> str:
    """截验证码图片并识别。

    截的是元素本身（screenshot_as_png）而不是整页：整页截图还得自己按坐标裁，
    页面一滚动或元素被其它东西盖住就裁错，而元素截图是浏览器直接给的，
    不存在对不齐的问题。
    """
    try:
        png = image_el.screenshot_as_png
    except WebDriverException as e:
        log.warning("验证码截图失败: %s", e)
        return ""

    if not png:
        log.warning("验证码截图是空的")
        return ""

    try:
        text = captcha.recognize(png)
    except Exception as e:
        # ddddocr 遇到畸形图片会抛，这不该让整轮尝试崩掉——按"这次没认出来"处理。
        log.warning("验证码识别失败: %s", e)
        return ""

    log.info("验证码识别结果: %s（%d 字节的截图）", text, len(png))
    return text


def _attempt_loop(driver, cfg: Config, password: str, nav_timed_out: bool) -> Result:
    """按 cfg.captcha_max_retry 的次数上限重试。

    次数上限是这里最重要的安全属性：学校连续失败会锁账号，所以无论如何都不会
    多试一次，也不会在两次之间做无谓的重复尝试。
    """
    attempts = max(1, cfg.captcha_max_retry)
    image_el = None
    reload_el = None
    last = Result(False, "captcha_failed", "没有执行任何尝试")

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            # 换一张验证码再试（优先点"换一个"控件，点图片会被它拦掉）
            _refresh_captcha(driver, reload_el, image_el)

        last, image_el, reload_el = _attempt(driver, cfg, password, nav_timed_out, attempt)

        if last.ok:
            log.info("登录成功（第 %d/%d 次尝试）", attempt, attempts)
            return last

        if last.reason != "captcha_failed":
            # bad_credentials / not_login_page / timeout 都是确定性的：用同样的方式
            # 再试一次只会重复同样的失败，而每次失败都在消耗锁号预算。
            log.warning("停止重试（%s）", last.reason)
            return last

        if attempt < attempts:
            log.warning("第 %d/%d 次尝试未成功，换一张验证码再试", attempt, attempts)

    log.error("已尝试 %d 次仍未成功，停止", attempts)
    return last


def _run(cfg: Config, password: str) -> Result:
    profile = tempfile.mkdtemp(prefix="campus-net-profile-")
    driver = None
    try:
        driver = _build_driver(cfg, profile)
        nav_timed_out = _navigate(driver, cfg)
        return _attempt_loop(driver, cfg, password, nav_timed_out)
    finally:
        # 浏览器一定要关：留着它用户会看到一个莫名多出来的窗口，而且下次运行
        # 如果复用了同一个配置目录就会起不来。
        if driver is not None:
            _quit(driver)
        # 临时配置目录不清会越积越多（每个几十 MB），删不掉也不能影响登录结果。
        shutil.rmtree(profile, ignore_errors=True)


def login(cfg: Config) -> Result:
    """打开登录页完成一次认证。

    任何异常都会变成 Result，绝不外抛——脚本由计划任务拉起，stderr 是丢掉的，
    一个未捕获的栈回溯等于什么都没发生，用户只会看到"网又没登上"。
    """
    try:
        password = secret.load_password(cfg.secret_path)
    except secret.SecretError as e:
        # 密码文件读不出来：这次运行**根本没有发出登录请求**。
        # 所以单独一个 reason，不能混进 captcha_failed——那样会被记进锁号窗口，
        # 反复触发"账号要被锁了"的假警报。
        log.error("读取密码失败: %s", e)
        return Result(False, "secret", str(e))

    log.info("已读取密码文件 %s（内容不记录）", cfg.secret_path)

    try:
        return _run(cfg, password)
    except _PasswordSendError as e:
        # 只带 WebDriver 的异常类名，不带内容 —— 见 _type_password
        log.error("密码输入失败（WebDriver: %s）", e)
        return Result(False, "browser_error", f"密码输入失败（WebDriver: {e}）")
    except WebDriverException as e:
        log.error("浏览器操作失败: %s", e)
        return Result(False, "browser_error", f"{type(e).__name__}: {e}")
    except OSError as e:
        # 临时目录建不出来、驱动文件读不了之类
        log.error("浏览器环境异常: %s", e)
        return Result(False, "browser_error", f"{type(e).__name__}: {e}")
    except Exception as e:
        # 兜底：把栈写进日志文件（那里才是事后唯一能查的东西），
        # 返回给调用方的是一句人话。
        log.exception("未预期的错误")
        return Result(False, "browser_error", f"{type(e).__name__}: {e}")


def dump_form(cfg: Config) -> Result:
    """--dump-form 用：打开登录页，把采集到的元素和自动识别结果渲染进 detail。

    这是给用户（非程序员）校准 selectors 的入口：自动识别没对上时，让他把这个
    输出贴回来，就能知道页面到底长什么样。
    """
    profile = tempfile.mkdtemp(prefix="campus-net-profile-")
    driver = None
    try:
        driver = _build_driver(cfg, profile)
        nav_timed_out = _navigate(driver, cfg)
        _elements, descriptors = _collect(driver)

        controls = [d for d in descriptors if d["tag"] != "img"]
        images = [d for d in descriptors if d["tag"] == "img"]

        try:
            title = driver.title or ""
        except WebDriverException:
            title = ""

        parts = [
            f"当前地址: {_current_url(driver)}",
            f"页面标题: {title}",
            "",
            "=== 页面上的表单元素（按文档顺序）===",
            render_elements(descriptors),
            "",
            "=== 自动识别结果 ===",
            render_verdict(controls, images),
            "",
            "=== config.json 里配置的 selectors ===",
            render_overrides(driver, cfg.selectors),
        ]
        if nav_timed_out:
            parts += ["", "注意：这次页面加载超时了，采到的元素可能不完整。"]
        return Result(True, "success", "\n".join(parts))
    except WebDriverException as e:
        log.error("打开登录页失败: %s", e)
        return Result(False, "browser_error", f"{type(e).__name__}: {e}")
    except OSError as e:
        log.error("浏览器环境异常: %s", e)
        return Result(False, "browser_error", f"{type(e).__name__}: {e}")
    finally:
        if driver is not None:
            _quit(driver)
        shutil.rmtree(profile, ignore_errors=True)
