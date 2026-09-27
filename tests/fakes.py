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
