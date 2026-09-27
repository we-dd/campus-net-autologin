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
    """钉死 dwFlags：两个方向都必须传 CRYPTPROTECT_UI_FORBIDDEN（1）。

    传 0 会永久阻塞的原因见 src/secret.py 里那个常量的注释，这里不重复，
    免得同一个理由要在两处维护。断言用的是字面量 1（不是常量本身），
    这样连"常量被改成别的值"也一起钉住。
    """
    real = secret.crypt32
    seen = []

    class SpyCrypt32:
        """记下 dwFlags（第 6 个位置参数，下标 5），其余原样转发。"""

        def CryptProtectData(self, *args):
            assert len(args) == 7, f"CryptProtectData 该传 7 个位置参数，实际 {len(args)}"
            seen.append(("CryptProtectData", args[5]))
            return real.CryptProtectData(*args)

        def CryptUnprotectData(self, *args):
            assert len(args) == 7, f"CryptUnprotectData 该传 7 个位置参数，实际 {len(args)}"
            seen.append(("CryptUnprotectData", args[5]))
            return real.CryptUnprotectData(*args)

    monkeypatch.setattr(secret, "crypt32", SpyCrypt32())

    p = str(tmp_path / "secret.dat")
    save_password("hunter2", p)
    assert load_password(p) == "hunter2"

    assert seen == [("CryptProtectData", 1), ("CryptUnprotectData", 1)]
    assert secret._CRYPTPROTECT_UI_FORBIDDEN == 1, "Win32 里 CRYPTPROTECT_UI_FORBIDDEN 就是 1"


def test_load_directory_raises_secret_error(tmp_path):
    """路径是目录时 open() 抛 OSError，必须转成 SecretError，调用方只 catch 它。"""
    with pytest.raises(SecretError, match="读取失败"):
        load_password(str(tmp_path))


def test_save_to_directory_raises_secret_error(tmp_path):
    with pytest.raises(SecretError, match="写入失败"):
        save_password("hunter2", str(tmp_path))


def test_non_utf8_blob_raises_secret_error(tmp_path):
    """密文解得开、内容却不是 UTF-8 时，同样要变成 SecretError。

    以前这里漏成 UnicodeDecodeError：它不是 SecretError，会漏过 portal.login 的
    兜底，冒到 login.py 的通用 except（那里不弹窗），计划任务下就成了静默失败 ——
    用户只看到网络一直是断的，没有任何提示。
    """
    p = tmp_path / "secret.dat"
    p.write_bytes(secret._protect(b"\xff\xfe\x80 not utf-8"))
    with pytest.raises(SecretError, match="不是 UTF-8"):
        load_password(str(p))


def test_path_with_nul_raises_secret_error():
    """手改的 config.json 里 secret_path 可能带 NUL，open() 抛的是 ValueError。"""
    with pytest.raises(SecretError, match="写入失败"):
        save_password("hunter2", "a\0b.dat")


def test_surrogate_password_raises_secret_error(tmp_path):
    """孤立代理字符（重定向标准输入绕开 getpass 时可能出现）encode 会抛 UnicodeEncodeError。"""
    with pytest.raises(SecretError, match="写入失败"):
        save_password("\ud800", str(tmp_path / "secret.dat"))
