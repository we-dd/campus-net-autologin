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
# 0 表示允许 DPAPI 在需要时做交互式解析（弹 UI / 走凭据界面）。本脚本由计划任务
# 每 30 分钟以非交互方式拉起，一旦 secret.dat 头部 dwFlags 字段（偏移 40，
# 共 4 字节）被改坏哪怕一个 bit，CryptUnprotectData 就会一直阻塞、永不返回
# （实测 25 秒仍未返回，且不占 CPU，不是死循环），直到计划任务的执行上限把进程
# 杀掉 —— 于是每次运行都登不上网。传入 CRYPTPROTECT_UI_FORBIDDEN 后，同样的
# 文件会在 2 毫秒内返回错误 1325，由调用方按普通解密失败处理。
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


def _unprotect(data: bytes) -> bytes:
    in_blob, _keep = _make_blob(data)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        # 错误码随损坏方式而变，没有哪个单独指向"账户不对"（本机实测：非 DPAPI 密文
        # → 87，文件被截断 → 87，密文正文被改坏 → 13；别处也见过 -2146893813）。
        # 所以先说最可能的原因和对策，"换过账户"只作为另一种可能提一句 ——
        # 否则用户会去找一个并不存在的账户问题。
        raise SecretError(
            f"解密失败，错误码 {ctypes.get_last_error()}。"
            "密码文件读不出来，多半是文件已损坏或不是本程序生成的，"
            "请重新运行 python login.py --setup 生成一个；"
            "若这个文件是从别的 Windows 账户或别的机器上拷来的，同样解不开，需要在本机重新生成。"
        )
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_password(password: str, path: str) -> None:
    blob = _protect(password.encode("utf-8"))
    # 目录或文件不可写时 open() 抛的是 OSError（如把目录当文件写、没权限），
    # 调用方只 catch SecretError，所以这里统一转成 SecretError。
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
    except OSError as e:
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
    return _unprotect(blob).decode("utf-8")
