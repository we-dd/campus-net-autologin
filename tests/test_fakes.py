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
