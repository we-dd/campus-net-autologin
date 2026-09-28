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


def test_unauthed_when_portal_reachable_but_probes_unexpected():
    """门户可达 + 外网响应不符合预期 → 未认证。

    这条曾经断言 UNKNOWN，那是错的：门户可达就说明我们在校园网里，外网不通
    只可能是没认证。判成 UNKNOWN 会导致不登录、干等下一轮，实测表现就是
    「开机后网络一直连不上，最后只能手动登」。
    """
    s = FakeSession({
        PROBE1: FakeResponse(500, "server error", headers={"Content-Type": "text/plain"}),
        PROBE2: FakeResponse(500, "server error", headers={"Content-Type": "text/plain"}),
        PORTAL: FakeResponse(200, "<html>portal</html>"),
    })
    assert check(cfg(), s) is State.UNAUTHED


def test_unauthed_when_intercept_returns_200_without_content_type():
    """最贴近实测形态的一条：AC 回 200、没有 Content-Type、正文是拦截内容。

    它不是 302（所以不像劫持），也不匹配 expect_text（所以不符合预期）。
    旧逻辑在这里判 UNKNOWN，正是线上踩到的那个坑。
    """
    s = FakeSession({
        PROBE1: FakeResponse(200, "<script>location.href='http://portal/'</script>", headers={}),
        PROBE2: FakeResponse(200, "", headers={}),
        PORTAL: FakeResponse(200, "", headers={}),
    })
    assert check(cfg(), s) is State.UNAUTHED


def test_no_unknown_state_anymore():
    """UNKNOWN 状态已被移除。

    它的语义是"说不清所以什么都别做"，但实测只在"其实该登录"的时候触发，
    导致开机后网络一直不恢复。宁可多试一次（有防锁号兜底），也不要该动手时不动手。
    """
    assert not hasattr(State, "UNKNOWN")
    assert {s.value for s in State} == {"online", "unauthed", "portal_down"}


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
