"""验证码识别。

用 ddddocr 在本地识别，不联网、不落盘：整个模块只有一个入口，
输入是图片字节，输出是识别出来的字符串。

模型加载是这里唯一贵的地方（onnx 会话初始化），所以做成模块级单例：
login.py 一次运行可能识别好几次验证码，每次都重建模型会白白多花几秒。
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

# 模型对象。首次用时才建，之后一直复用（见 _get_ocr）。
_ocr = None
# 预热线程和主线程可能同时走到初始化，必须加锁，否则会建两次 ONNX 会话。
_ocr_lock = threading.Lock()


def _get_ocr():
    """懒加载 ddddocr 单例。

    show_ad=False 是必须的：默认值会在导入时打印一段带链接的广告横幅，
    计划任务下没人看，还会混进日志。
    """
    global _ocr
    if _ocr is not None:
        return _ocr
    with _ocr_lock:
        if _ocr is None:
            log.debug("加载 ddddocr 模型（首次识别会慢一点）")
            import ddddocr

            _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


def warm_up() -> None:
    """预加载模型，供后台线程调用。

    默认模型 common.onnx 有 51.6 MB，开机/唤醒后第一次读它要几秒（冷文件缓存 +
    杀毒扫描）。而浏览器启动、页面稳定、表单采集加起来也要好几秒 —— 让模型加载
    和它们并行，等真要识别时通常已经就绪。

    预热失败不影响主流程：recognize() 到时候还会再试一次。
    """
    try:
        _get_ocr()
    except Exception as e:
        log.debug("预加载 ddddocr 失败（不影响主流程）: %s", e)


def recognize(image_bytes: bytes) -> str:
    """识别一张验证码图片，返回识别结果（可能为空串）。

    空串是正常结果（模型认不出时会返回空），调用方要按"这次多半会失败"处理，
    而不是当异常。图片格式不对时 ddddocr 会抛异常，这里不接——调用方
    （browser_login）统一兜底，免得多写一层只为了换个说法。
    """
    return _get_ocr().classification(image_bytes).strip()
