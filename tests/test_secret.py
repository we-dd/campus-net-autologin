import pytest

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
