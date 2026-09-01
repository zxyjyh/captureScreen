"""用 macOS 内置的 Vision 框架做本地 OCR。

存在的理由和 accessibility.py 一样是成本，但两者互补：
无障碍树准确、结构化，可只覆盖有可见窗口的应用（Chrome 一屏能给 5000+ 字）；
OCR 覆盖任何截得到的画面，代价是会把图标和菜单栏误读成碎字。

先试无障碍树，不够再 OCR，都不够才轮到多模态模型 —— 这个顺序是
按「准确度 × 成本」排的，不是随意的。

实测（2026-09-01，本机）：1920x1080 截图约 1.5 秒，155 行 / 947 字。
完全离线，不产生任何 API 费用。
"""

import Quartz
import Vision
from Foundation import NSURL

# 简体中文优先。顺序影响识别结果 —— Vision 会按这个顺序做语言假设。
DEFAULT_LANGUAGES = ("zh-Hans", "en-US")

# 低于这个置信度的行多半是图标或装饰被误读成字，留着只会污染检索。
MIN_CONFIDENCE = 0.3

# 实测置信度过滤几乎不起作用 —— Vision 对「园」「00」「四」这类图标误读
# 同样给高分。真正有效的是长度：1-2 字的行绝大多数是图标、角标、快捷键，
# 滤掉它们只损失 16% 字符，却去掉了 62 行噪音（本机 2026-09-01 实测）。
MIN_LINE_CHARS = 3


def recognize(image_path: str, languages=DEFAULT_LANGUAGES) -> list[str]:
    """对图片做 OCR，返回按置信度过滤后的文本行。失败返回空列表。"""
    url = NSURL.fileURLWithPath_(str(image_path))
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        return []

    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        return []

    request = Vision.VNRecognizeTextRequest.alloc().init()
    # Accurate 比 Fast 慢一倍但准得多；这是离线批处理，不差这一秒
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(list(languages))
    request.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, _ = handler.performRequests_error_([request], None)
    if not ok:
        return []

    lines: list[str] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates or not len(candidates):
            continue
        best = candidates[0]
        if best.confidence() < MIN_CONFIDENCE:
            continue
        text = " ".join(best.string().split())
        if len(text) >= MIN_LINE_CHARS:
            lines.append(text)
    return lines


def recognize_text(image_path: str, languages=DEFAULT_LANGUAGES) -> str:
    return "\n".join(recognize(image_path, languages))
