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


def is_trusted() -> bool:
    """无障碍权限是否已授予。没有权限时整棵树都读不到。"""
    return bool(AS.AXIsProcessTrusted())
