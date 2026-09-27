"""用 Windows DPAPI 加密保存密码。

不引入 keyring / pywin32，直接用 ctypes 调 crypt32。
加密结果绑定当前 Windows 账户：换账户或换机器都解不开，这是预期行为。
"""
from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

log = logging.getLogger(__name__)

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL

crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL

kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
kernel32.LocalFree.restype = wintypes.HLOCAL


# DPAPI 的 dwFlags 参数。绝不能传 0。
#
# 0 允许 DPAPI 做交互式解析（弹 UI / 走凭据界面）。本脚本由计划任务每 30 分钟以
# 非交互方式拉起，一旦 secret.dat 头部 dwFlags 字段（偏移 40，共 4 字节）里
# CRYPTPROTECT_PROMPT_ON_UNPROTECT / ON_PROTECT 这两位（0x1、0x2）被置起来，
# CryptUnprotectData 就会一直阻塞、永不返回（实测 25 秒未返回，且不占 CPU，
# 不是死循环），直到计划任务把进程杀掉 —— 于是每次运行都登不上网。
# 传 1 之后，同一份文件 13 毫秒内返回错误 1325，按普通解密失败处理。
# （8 种单 bit 模式都扫过：只有 0x1、0x2 这两个 PROMPT 位会卡住，
#  0x04 / 0x08 / 0x10 / 0x20 / 0x40 / 0x80 都快速报错。）
#
# 别被密文骗了：DPAPI 不会把 UI_FORBIDDEN 写回文件，正常的 secret.dat 在偏移 40
# 处读出来仍然是 00 00 00 00。看到 0 不代表这个常量没生效，不要据此改回 0。
_CRYPTPROTECT_UI_FORBIDDEN = 1


class SecretError(Exception):
    """密码文件的读写或加解密失败。"""


def _make_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf  # 返回 buf 是为了保持引用，防止被 GC


def _protect(data: bytes) -> bytes:
    in_blob, _keep = _make_blob(data)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), "campus-net", None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretError(f"加密失败，错误码 {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect(data: bytes, path: str) -> bytes:
    in_blob, _keep = _make_blob(data)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        # 错误码随损坏方式而变（实测 87 / 13 / -2146893813 都出现过），没有哪个单独
        # 指向"账户不对"。用户看不懂错误码，所以先把最常见的原因和对策说清楚，
        # 代码放最后括号里备查。
        raise SecretError(
            f"密码文件解密失败: {path}。"
            "多半是文件已损坏或不是本程序生成的，请重新运行 python login.py --setup 重新生成；"
            "从别的 Windows 账户或别的机器上拷来的文件同样解不开。"
            f"（错误码 {ctypes.get_last_error()}）"
        )
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_password(password: str, path: str) -> None:
    # 这一段里的失败都转成 SecretError：调用方（login.py --setup）只 catch 它。
    # password.encode 会抛 UnicodeEncodeError（孤立代理字符，比如从重定向的标准输入
    # 混进来的 \ud800），路径里有 NUL 时 open 抛 ValueError，权限不足或把目录当
    # 文件写时抛 OSError。
    try:
        blob = _protect(password.encode("utf-8"))
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
    except (OSError, ValueError) as e:
        raise SecretError(f"密码写入失败: {path}（{e}）") from e
    log.info("密码已加密保存到 %s", path)


def load_password(path: str) -> str:
    if not os.path.exists(path):
        raise SecretError(f"找不到密码文件: {path}。请先运行 python login.py --setup")
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        # 路径是目录、或被占用 / 没权限时，open() 抛 OSError 而不是 SecretError
        raise SecretError(f"密码文件读取失败: {path}（{e}）") from e
    if not blob:
        raise SecretError(f"密码文件是空的: {path}。请重新运行 python login.py --setup")
    try:
        return _unprotect(blob, path).decode("utf-8")
    except UnicodeDecodeError as e:
        # 密文能解开却不是 UTF-8：本模块写进去的一定是 UTF-8，所以这文件不是本程序生成的。
        # 不接住的话 UnicodeDecodeError 会漏过 portal.login 的 SecretError 兜底，一路冒到
        # login.py 的通用 except（那里不弹窗），在计划任务下变成静默失败。
        raise SecretError(
            f"密码文件解密失败: {path}。解出来的内容不是 UTF-8 文本，多半不是本程序生成的，"
            "请重新运行 python login.py --setup 重新生成。"
        ) from e
