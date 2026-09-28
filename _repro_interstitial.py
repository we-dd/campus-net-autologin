"""尝试在本地复现 Chrome 的强制门户提示页。

思路：Chrome 的强制门户检测靠它自己探测 www.gstatic.com 之类的连通性地址。
用 --host-resolver-rules 把那些域名指到本地服务器，服务器返回"像门户"的响应，
看 Chrome 会不会弹它自己的提示页。

如果复现成功，就能在本地把提示页那一段彻底验证掉，不用等真实断网。
用完删掉。
"""
import http.server
import logging
import socketserver
import tempfile
import threading
import time

for name in ("selenium", "urllib3", "websocket"):
    logging.getLogger(name).setLevel(logging.WARNING)

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

PORTAL_HOST = "portal.test"


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        # 连通性探测：返回 302 到门户 —— 这正是 AC 拦截时的行为
        if host in ("www.gstatic.com", "connectivitycheck.gstatic.com",
                    "www.msftconnecttest.com", "connect.rom.miui.com"):
            self._send(302, extra={"Location": "http://%s/" % PORTAL_HOST})
            return
        # 门户自己：给一个普通页面，看 Chrome 有没有先弹提示页挡住它
        self._send(200, b"<html><head><meta charset='utf-8'><title>PORTAL-PAGE</title></head>"
                        b"<body><h1>PORTAL-PAGE</h1></body></html>")

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("本地服务器端口:", port)

rules = ",".join(
    "MAP %s 127.0.0.1:%d" % (h, port)
    for h in ("www.gstatic.com", "connectivitycheck.gstatic.com",
              "www.msftconnecttest.com", "connect.rom.miui.com", PORTAL_HOST)
)

profile = tempfile.mkdtemp(prefix="campus-net-repro-")
opts = Options()
opts.add_argument("--user-data-dir=" + profile)
opts.add_argument("--no-first-run")
opts.add_argument("--no-default-browser-check")
opts.add_argument("--no-proxy-server")
opts.add_argument("--host-resolver-rules=" + rules)

driver = None
try:
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(20)
    driver.get("http://%s/" % PORTAL_HOST)
    time.sleep(6)

    print()
    print("落地 URL :", driver.current_url)
    print("标题     :", repr(driver.title))
    has_primary = bool(driver.find_elements(By.CSS_SELECTOR, "button#primary-button"))
    has_details = bool(driver.find_elements(By.CSS_SELECTOR, "button#details-button"))
    print("有 #primary-button:", has_primary)
    print("有 #details-button:", has_details)
    try:
        print("正文开头 :", repr((driver.find_element(By.CSS_SELECTOR, "body").text or "")[:200]))
    except Exception:
        pass

    from src import browser_login as bl
    print()
    print("★ 是不是 Chrome 提示页:", bl._detect_captive_portal_interstitial(driver))
except Exception as e:
    import traceback
    print("!!! 出错:", type(e).__name__, e)
    traceback.print_exc()
finally:
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
    srv.shutdown()
