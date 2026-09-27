import pytest

from src import secret
from src.secret import SecretError, load_password, save_password


def test_roundtrip(tmp_path):
    p = str(tmp_path / "secret.dat")
    save_password("hunter2", p)
    assert load_password(p) == "hunter2"


def test_roundtrip_non_ascii(tmp_path):
    p = str(tmp_path / "secret.dat")
    save_password("密码abc123!@#", p)
    assert load_password(p) == "密码abc123!@#"


def test_file_does_not_contain_plaintext(tmp_path):
    p = tmp_path / "secret.dat"
    save_password("hunter2", str(p))
    assert b"hunter2" not in p.read_bytes()


def test_missing_file_raises_readable_error(tmp_path):
    with pytest.raises(SecretError, match="找不到密码文件"):
        load_password(str(tmp_path / "nope.dat"))


def test_corrupt_file_raises_readable_error(tmp_path):
    p = tmp_path / "secret.dat"
    p.write_bytes(b"this is not a dpapi blob")
    with pytest.raises(SecretError, match="解密失败"):
        load_password(str(p))


def test_both_calls_forbid_ui(monkeypatch, tmp_path):
    """钉死 dwFlags 必须是 CRYPTPROTECT_UI_FORBIDDEN（1），两个方向都要。

    传 0 等于允许 DPAPI 做交互式解析：只要 secret.dat 头部 dwFlags 字段
    （偏移 40）被改坏一个 bit，CryptUnprotectData 就会一直阻塞不返回
    （实测 25 秒仍未返回，且不占 CPU）；换成 1 之后，同一个文件 2 毫秒内
    返回错误 1325。本脚本由计划任务每 30 分钟拉起一次，卡住就等于永远登不上网，
    所以这个参数必须钉死，不能让以后的重构悄悄改回 0。
    """
    real = secret.crypt32
    seen = []

    class SpyCrypt32:
        """把 dwFlags（第 6 个位置参数，下标 5）记下来，其余原样转发。"""

        def CryptProtectData(self, *args):
            seen.append(("CryptProtectData", args[5]))
            return real.CryptProtectData(*args)

        def CryptUnprotectData(self, *args):
            seen.append(("CryptUnprotectData", args[5]))
            return real.CryptUnprotectData(*args)

    monkeypatch.setattr(secret, "crypt32", SpyCrypt32())

    p = str(tmp_path / "secret.dat")
    save_password("hunter2", p)
    assert load_password(p) == "hunter2"

    assert seen == [("CryptProtectData", 1), ("CryptUnprotectData", 1)]


def test_load_directory_raises_secret_error(tmp_path):
    """路径是目录时 open() 抛 OSError，必须转成 SecretError，调用方只 catch 它。"""
    with pytest.raises(SecretError, match="读取失败"):
        load_password(str(tmp_path))


def test_save_to_directory_raises_secret_error(tmp_path):
    with pytest.raises(SecretError, match="写入失败"):
        save_password("hunter2", str(tmp_path))
