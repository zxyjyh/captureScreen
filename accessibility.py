"""从 macOS 无障碍树抽取屏幕文本。

存在的理由是成本：截图过多模态模型每张都要花钱，而无障碍树是操作系统
本地免费给的结构化文本（按钮、标签、输入框的实际内容），比 OCR 还准。

实测（2026-09-01，本机）：
    Chrome 一屏 235 条 / 5245 字，访达 403 字，Ghostty 仅标签标题 125 字。
    没有可见窗口的应用取不到 —— 这是正常的，不是故障。

拿不到时返回空串，由调用方决定是否退回多模态分析。
"""

import ApplicationServices as AS
from AppKit import NSWorkspace

# 菜单栏对「我在做什么」没有信息量，且体量很大（Chrome 的菜单有 600+ 项），
# 不跳过会把预算全吃掉，还稀释后续检索。
SKIP_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"}

# 这几个属性覆盖了绝大多数可见文本：值、标题、无障碍描述。
TEXT_ATTRS = ("AXValue", "AXTitle", "AXDescription")

MAX_DEPTH = 14
MAX_CHILDREN_PER_NODE = 120
MAX_FRAGMENTS = 4000
MAX_FRAGMENT_CHARS = 800

# 方向隔离符：显示时不可见，落进文本里只会干扰匹配
_BIDI_STRIP = {c: None for c in range(0x2066, 0x206A)}


def _attr(element, name):
    err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def _walk(element, out: list[str], seen: set[str], budget: list[int], depth: int = 0):
    if depth > MAX_DEPTH or budget[0] <= 0:
        return
    if _attr(element, "AXRole") in SKIP_ROLES:
        return

    for name in TEXT_ATTRS:
        value = _attr(element, name)
        if not isinstance(value, str):
            continue
        # 界面文本常带换行和缩进，压平后才好判重和阅读
        text = " ".join(value.split())
        if 1 < len(text) < MAX_FRAGMENT_CHARS and text not in seen:
            seen.add(text)
            out.append(text)
            budget[0] -= 1

    for child in (_attr(element, "AXChildren") or [])[:MAX_CHILDREN_PER_NODE]:
        _walk(child, out, seen, budget, depth + 1)


def frontmost_app() -> tuple[int, str]:
    """返回 (pid, 应用名)。取不到时 pid 为 0。"""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return 0, ""
    return app.processIdentifier(), app.localizedName() or ""


def extract_text(pid: int, max_windows: int = 3) -> list[str]:
    """走该进程的可见窗口，返回去重后的文本片段。

    只走 AXWindows 而不是整个应用元素：应用元素的直接子节点里
    菜单栏占绝大多数，而窗口才是「用户正在看什么」。
    """
    if not pid:
        return []

    app_element = AS.AXUIElementCreateApplication(pid)
    windows = _attr(app_element, "AXWindows") or []

    out: list[str] = []
    seen: set[str] = set()
    budget = [MAX_FRAGMENTS]
    for window in windows[:max_windows]:
        _walk(window, out, seen, budget)
    return out


def capture_screen_text(pid: int | None = None) -> tuple[str, int]:
    """抓当前（或指定进程）的屏幕文本。返回 (文本, 片段数)。"""
    if pid is None:
        pid, _ = frontmost_app()
    fragments = extract_text(pid)
    return "\n".join(fragments), len(fragments)


def _focused_window(pid: int):
    """前台窗口元素。AXFocusedWindow 不是所有应用都支持（访达就返回
    -25212 属性不支持），所以退回窗口列表第一个。"""
    if not pid:
        return None
    app_element = AS.AXUIElementCreateApplication(pid)
    window = _attr(app_element, "AXFocusedWindow")
    if window is not None:
        return window
    windows = _attr(app_element, "AXWindows") or []
    return windows[0] if windows else None


def frontmost_window_title(pid: int | None = None) -> str:
    """前台窗口标题。拿不到返回空串。"""
    if pid is None:
        pid, _ = frontmost_app()
    window = _focused_window(pid)
    if window is None:
        return ""
    title = _attr(window, "AXTitle")
    if not isinstance(title, str):
        return ""
    # macOS 会在标题里插入 Unicode 方向隔离符（U+2066..U+2069），
    # 落进文本后既碍眼又干扰后续的关键词匹配
    return " ".join(title.translate(_BIDI_STRIP).split())


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    """前台窗口的 (x, y, 宽, 高)，全局坐标。取不到返回 None。

    用途是选对显示器：接了外接屏的时候，screencapture 默认只截主屏，
    而前台窗口可能整个在另一块屏上 —— 那样截出来的图和用户当时在看的
    东西毫无关系。
    """
    if pid is None:
        pid, _ = frontmost_app()
    window = _focused_window(pid)
    if window is None:
        return None

    pos, size = _attr(window, "AXPosition"), _attr(window, "AXSize")
    if pos is None or size is None:
        return None

    # AXValue 是不透明类型，要解包成 CGPoint / CGSize
    ok_p, point = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
    ok_s, dims = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_p and ok_s):
        return None
    return (point.x, point.y, dims.width, dims.height)


def is_trusted() -> bool:
    """无障碍权限是否已授予。没有权限时整棵树都读不到。"""
    return bool(AS.AXIsProcessTrusted())
