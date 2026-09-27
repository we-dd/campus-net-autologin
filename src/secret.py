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
        ctypes.byref(in_blob), "campus-net", None, None, None, 0, ctypes.byref(out_blob)
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
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise SecretError(
            f"解密失败，错误码 {ctypes.get_last_error()}。"
            "密码文件必须在加密它的同一个 Windows 账户下解密。"
        )
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_password(password: str, path: str) -> None:
    blob = _protect(password.encode("utf-8"))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)
    log.info("密码已加密保存到 %s", path)


def load_password(path: str) -> str:
    if not os.path.exists(path):
        raise SecretError(f"找不到密码文件: {path}。请先运行 python login.py --setup")
    with open(path, "rb") as f:
        blob = f.read()
    if not blob:
        raise SecretError(f"密码文件是空的: {path}。请重新运行 python login.py --setup")
    return _unprotect(blob).decode("utf-8")
