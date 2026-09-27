"""验证码识别。

刻意不断言识别出来的具体字符：ddddocr 对合成图片的输出本来就不确定，
钉死了就是一个会随机失败的测试。这里只钉"接口的形状"——不抛异常、返回 str、
空白被 strip 掉、模型是单例。
"""
import io

from PIL import Image, ImageDraw

from src import captcha


def _png(size=(90, 34), text="A7B3") -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    draw.text((4, 4), text, fill="black")
    draw.rectangle([0, 0, size[0] - 1, size[1] - 1], outline="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_recognize_returns_str():
    assert isinstance(captcha.recognize(_png()), str)


def test_recognize_result_is_stripped():
    """ddddocr 会返回带空白的串，而空白打进输入框就是识别错了，
    所以必须在模块里 strip 掉，而不是指望调用方记得。"""
    result = captcha.recognize(_png())
    assert result == result.strip()


def test_recognize_returns_str_for_non_captcha_images_too():
    """大图（比如 logo）也要能走同一条路：拿它当验证码去识别是调用方的判定错误，
    不该在这里变成异常。"""
    assert isinstance(captcha.recognize(_png(size=(400, 300))), str)


def test_ocr_model_is_loaded_only_once():
    """模型加载很贵，必须是模块级单例：login.py 一次运行可能识别好几次验证码。"""
    captcha.recognize(_png())
    first = captcha._get_ocr()
    captcha.recognize(_png())
    assert captcha._get_ocr() is first
