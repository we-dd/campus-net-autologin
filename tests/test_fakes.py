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
    assert jsonp('{"a":1}').endswith('({"a":1});')
