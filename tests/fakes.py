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
    """按调用顺序返回预置响应。

    routes 可以是：
      - dict: {url: FakeResponse | Exception | list[...]}，按 url 精确匹配
      - list: [(url_prefix, FakeResponse | Exception), ...]，按前缀匹配，先到先得
    匹配不到时抛 AssertionError，强迫测试显式声明所有请求。
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _pick(self, method, url):
        self.calls.append((method, url))
        if isinstance(self.routes, dict):
            entry = self.routes.get(url)
            if entry is None:
                raise AssertionError(f"未预期的请求: {method} {url}")
            if isinstance(entry, list):
                entry = entry.pop(0) if len(entry) > 1 else entry[0]
            return entry
        for prefix, entry in self.routes:
            if url.startswith(prefix):
                if isinstance(entry, list):
                    entry = entry.pop(0) if len(entry) > 1 else entry[0]
                return entry
        raise AssertionError(f"未预期的请求: {method} {url}")

    def _do(self, method, url, **kwargs):
        entry = self._pick(method, url)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def get(self, url, **kwargs):
        return self._do("GET", url, **kwargs)

    def post(self, url, data=None, **kwargs):
        return self._do("POST", url, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def jsonp(obj: str) -> str:
    """把 JSON 串包成锐捷风格的 JSONP 响应体。"""
    return f"jQuery1124000000000000000_1700000000000({obj});"
