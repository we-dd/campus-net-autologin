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
