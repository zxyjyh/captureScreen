def list_displays() -> list[tuple[int, object]]:
    """所有活动显示器，返回 [(screencapture 的 -D 序号, 边界), ...]。"""
    try:
        import Quartz
        err, ids, count = Quartz.CGGetActiveDisplayList(8, None, None)
        if err != 0:
            return []
        return [(i, Quartz.CGDisplayBounds(d)) for i, d in enumerate(ids[:count], start=1)]
    except Exception:
        return []


def _contains(bounds, x: float, y: float) -> bool:
    return (bounds.origin.x <= x < bounds.origin.x + bounds.size.width
            and bounds.origin.y <= y < bounds.origin.y + bounds.size.height)


def pick_display_index(pid: int | None = None) -> int | None:
    """前台窗口落在哪块显示器上（-D 序号，1 起）。判断不了返回 None。"""
    displays = list_displays()
    if len(displays) < 2:
        return None  # 单屏不用挑
    bounds = accessibility.frontmost_window_bounds(pid)
    if bounds is None:
        return None
    wx, wy, ww, wh = bounds
    cx, cy = wx + ww / 2, wy + wh / 2
    for idx, b in displays:
        if _contains(b, cx, cy):
            return idx
    return None


def app_on_display(bounds) -> str:
    """这块显示器上最上层窗口属于哪个应用。

    副屏上的内容不归前台应用管 —— 前台应用只有一个，而副屏往往开着
    另一个应用。NSWorkspace 回答不了「那块屏上是什么」，
    CGWindowList 按窗口位置筛选可以。
    """
    try:
        import Quartz
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowLayer") != 0:
                continue
            r = w.get("kCGWindowBounds") or {}
            cx = r.get("X", 0) + r.get("Width", 0) / 2
            cy = r.get("Y", 0) + r.get("Height", 0) / 2
            if _contains(bounds, cx, cy):
                return w.get("kCGWindowOwnerName") or ""
    except Exception:
        pass
    return ""


"""截图采集：定时截屏，并在同一时刻抓下无障碍树文本。

为什么截图的同时要抓无障碍文本：无障碍树只能读「此刻」的界面，
事后拿着 PNG 是抓不回来的。而它是本地免费的结构化文本
（Chrome 一屏 5000+ 字），能让下游分析少调甚至不调多模态模型。

采集时不做 OCR —— OCR 可以事后对着 PNG 补跑，放在采集主循环里
只会拖慢节奏、占用前台资源。
"""

import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import schedule
import yaml

import accessibility

SCRIPT_DIR = Path(__file__).resolve().parent

# launchd / cron 启动时不继承 shell 环境，凭证只能从 .env 取
import env_file  # noqa: E402

env_file.load()
PID_FILE = SCRIPT_DIR / "capture.pid"


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def frontmost() -> tuple[int, str, str]:
    """一次拿到 (pid, 应用名, 窗口标题)，三者必然出自同一个应用。

    以前应用名和窗口标题各查一次 osascript，无障碍文本又用 NSWorkspace
    单独查第三次 —— 三次调用中间隔着一次截图，用户切个应用就会把
    A 应用的标题配上 B 应用的正文，而且完全静默。实测出现过
    「Finder 快速查看」那一帧配着 Chrome 页面文本。

    顺带甩掉 osascript：一次 0.28 秒，NSWorkspace 是 0.0002 秒。
    """
    pid, name = accessibility.frontmost_app()
    return pid, name, accessibility.frontmost_window_title(pid)


PAUSE_FILE = SCRIPT_DIR / ".paused-until"


def paused_seconds() -> int:
    """暂停还剩多少秒，没暂停返回 0。

    用带过期时间的哨兵文件而不是让人去停服务：忘记恢复比忘记暂停常见得多，
    「我关了它然后三周没开」等于这个工具不存在。到点自动恢复。
    """
    try:
        until = float(PAUSE_FILE.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return 0
    left = int(until - time.time())
    if left <= 0:
        PAUSE_FILE.unlink(missing_ok=True)
        return 0
    return left


def screen_unavailable() -> str:
    """屏幕上没有可记录的东西时，返回原因；可以正常采集时返回空串。

    锁屏和显示器休眠时截出来的是纯黑图，无障碍树也只剩十来个字。
    这类帧不但没信息量，还会主动造成损害：文本太薄会触发多模态兜底，
    于是模型对着黑屏编出「用户可能处于任务准备阶段」这种内容，
    再被索引进检索库污染后续查询。实测一夜攒了 9 张锁屏截图和
    一份完全虚构的报告。

    空闲时长挡不住这种情况 —— 机器锁屏后会周期性唤醒，
    HIDIdleTime 跟着归零。
    """
    try:
        import Quartz
        # 这个键只在锁屏时存在，解锁状态下取不到
        session = Quartz.CGSessionCopyCurrentDictionary()
        if session and session.get("CGSSessionScreenIsLocked"):
            return "screen locked"
        if Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()):
            return "display asleep"
    except Exception:
        pass
    return ""


def get_idle_seconds() -> float:
    """系统空闲了多久。锁屏、离开工位时截图没有任何信息量，只是烧钱。"""
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "HIDIdleTime" in line:
                return int(line.rsplit("=", 1)[1].strip()) / 1_000_000_000
    except Exception:
        pass
    return 0.0


def is_sensitive_window(title: str, app_name: str, skip_keywords: list[str]) -> bool:
    """判断当前窗口是否包含敏感内容"""
    combined = f"{title} {app_name}".lower()
    for keyword in skip_keywords:
        if keyword.lower() in combined:
            return True
    return False


def pick_display_index(pid: int | None = None) -> int | None:
    """返回前台窗口所在显示器的 screencapture 序号（1 起）。判断不了就返回 None。

    为什么需要：screencapture 默认只截主显示器。接了外接屏之后，
    前台窗口可能整个在副屏上，截出来的图就和用户当时看的东西完全无关 ——
    而且是静默的，日志一切正常，只有翻报告时才发现驴唇不对马嘴。
    """
    try:
        import Quartz
        bounds = accessibility.frontmost_window_bounds(pid)
        if bounds is None:
            return None
        wx, wy, ww, wh = bounds
        cx, cy = wx + ww / 2, wy + wh / 2

        err, ids, count = Quartz.CGGetActiveDisplayList(8, None, None)
        if err != 0 or count < 2:
            return None  # 单屏不用挑
        for i, did in enumerate(ids[:count], start=1):
            b = Quartz.CGDisplayBounds(did)
            if (b.origin.x <= cx < b.origin.x + b.size.width
                    and b.origin.y <= cy < b.origin.y + b.size.height):
                return i
    except Exception:
        return None
    return None


def capture_screenshot(
    output_dir: Path,
    privacy_config: dict | None = None,
    idle_skip_seconds: float = 300.0,
    capture_all: bool = True,
):
    now = datetime.now()

    left = paused_seconds()
    if left:
        print(f"[{now.strftime('%H:%M:%S')}] Skipped: paused ({left//60}分{left%60}秒后恢复)")
        return

    reason = screen_unavailable()
    if reason:
        print(f"[{now.strftime('%H:%M:%S')}] Skipped: {reason}")
        return

    idle = get_idle_seconds()
    if idle_skip_seconds and idle >= idle_skip_seconds:
        print(f"[{now.strftime('%H:%M:%S')}] Skipped: idle {int(idle)}s")
        return

    pid, app_name, title = frontmost()

    # 兜底：不同 macOS 版本上锁屏信号不总是可靠，但前台是 loginwindow
    # 就一定是锁屏界面
    if app_name == "loginwindow":
        print(f"[{now.strftime('%H:%M:%S')}] Skipped: login window")
        return

    # 隐私检查：跳过敏感窗口
    if privacy_config and privacy_config.get("enabled", False):
        skip_keywords = privacy_config.get("skip_keywords", [])
        if is_sensitive_window(title, app_name, skip_keywords):
            print(f"[{now.strftime('%H:%M:%S')}] Skipped: sensitive ({app_name}: {title[:30]})")
            return

    date_dir = output_dir / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    filename = now.strftime("%H-%M-%S")
    filepath = date_dir / f"{filename}.png"

    displays = list_displays()
    active = pick_display_index(pid)

    def shoot(target: int | None, path: Path):
        cmd = ["screencapture", "-x", "-t", "png"]
        if target is not None:
            cmd += ["-D", str(target)]
        cmd.append(str(path))
        subprocess.run(cmd, check=True, capture_output=True)

    shoot(active, filepath)

    # 无障碍文本必须在截图的同一时刻抓 —— 界面变了就再也读不到了
    ax_chars = 0
    try:
        ax_text, _ = accessibility.capture_screen_text(pid)
        if ax_text:
            (date_dir / f"{filename}.txt").write_text(ax_text)
            ax_chars = len(ax_text)
    except Exception as e:
        print(f"[{now.strftime('%H:%M:%S')}] accessibility failed: {e}")

    # 元数据要在无障碍抽取之后写：前台应用可能最小化或只剩托盘图标，
    # 这时选屏拿不到窗口位置、退回主屏，拍到的其实是别的应用。
    # 照直写应用名就是撒谎，下游会把 A 的名字配上 B 的画面 ——
    # 实测出现过「钉钉」那一帧的正文全是 VS Code。
    readable = bool(title) or ax_chars > 0 or active is not None
    label = app_name if readable else f"{app_name}(无可读窗口)"
    (date_dir / f"{filename}.meta").write_text(f"{label}\n{title}\npid={pid}")

    # 副屏：前台应用只有一个，但副屏上往往开着另一个应用，
    # 那块屏上的内容同样是「用户当时看得见的东西」。
    # 无障碍树只覆盖前台应用，副屏的文字只能靠分析时的 OCR。
    extra = 0
    if capture_all and len(displays) > 1:
        for idx, bounds in displays:
            if idx == active:
                continue
            side = date_dir / f"{filename}-s{idx}.png"
            try:
                shoot(idx, side)
            except subprocess.CalledProcessError:
                continue
            side_app = app_on_display(bounds)
            side.with_suffix(".meta").write_text(f"{side_app}\n\npid=")
            extra += 1

    screen = f" | 屏{active}" if active is not None else ""
    more = f" +{extra}屏" if extra else ""
    print(f"[{now.strftime('%H:%M:%S')}] {app_name} | {title[:40]} | ax={ax_chars}字{screen}{more}")


def cleanup_old_reports(report_dir: Path, retention_days: int):
    """按保留期删过期报告。

    原来只清截图，报告永久保留 —— 而报告是被模型浓缩过的内容，
    信息密度比单张截图高得多，留得越久风险越大。
    0 表示不清理。
    """
    if not retention_days or not report_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    for date_dir in report_dir.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue  # weekly 这类非日期目录不动
        if dir_date >= cutoff:
            continue
        for f in date_dir.iterdir():
            f.unlink()
        date_dir.rmdir()
        print(f"Cleaned up report {date_dir}")


def cleanup_old_screenshots(output_dir: Path, report_dir: Path, retention_days: int):
    """只删除已经成功生成报告的截图，未分析的截图保留"""
    cutoff = datetime.now() - timedelta(days=retention_days)

    for date_dir in output_dir.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue

        # 超过保留期才清理
        if dir_date >= cutoff:
            continue

        # 只删除已成功生成报告的日期目录
        report_date_dir = report_dir / date_dir.name
        if report_date_dir.exists():
            for f in date_dir.iterdir():
                f.unlink()
            date_dir.rmdir()
            print(f"Cleaned up {date_dir} (reports exist)")
        else:
            print(f"Skipping {date_dir} (no reports yet, keeping screenshots)")


def run_hourly_analysis():
    """自动分析上一个小时的截图"""
    now = datetime.now()
    prev = now - timedelta(hours=1)
    python = str(SCRIPT_DIR / "venv" / "bin" / "python")
    analyze_script = str(SCRIPT_DIR / "analyze.py")
    try:
        result = subprocess.run(
            [python, analyze_script, "--date", prev.strftime("%Y-%m-%d"), "--hour", str(prev.hour)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            print(f"[{now.strftime('%H:%M:%S')}] Auto analysis done for {prev.strftime('%Y-%m-%d %H')}:00")
        else:
            print(f"[{now.strftime('%H:%M:%S')}] Auto analysis failed: {result.stderr[:100]}")
    except Exception as e:
        print(f"[{now.strftime('%H:%M:%S')}] Auto analysis error: {e}")


def write_pid():
    PID_FILE.write_text(str(os.getpid()))


def remove_pid():
    PID_FILE.unlink(missing_ok=True)


def handle_signal(signum, frame):
    print("\nStopping capture...")
    remove_pid()
    sys.exit(0)


def main():
    # 截图和屏幕文本是这台机器上最敏感的文件之一，默认 644 意味着
    # 同机器的任何进程、任何其他用户都能读。收紧到「只有自己」。
    os.umask(0o077)

    config = load_config()
    capture_config = config["capture"]
    interval = capture_config["interval_minutes"]
    output_dir = SCRIPT_DIR / capture_config["output_dir"]
    retention_days = capture_config["retention_days"]
    report_retention = config["report"].get("retention_days", 0)
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    privacy_config = config.get("privacy")
    idle_skip = float(capture_config.get("idle_skip_seconds", 300))
    capture_all = capture_config.get("displays", "all") == "all"

    output_dir.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    write_pid()

    capture_screenshot(output_dir, privacy_config, idle_skip, capture_all)
    cleanup_old_screenshots(output_dir, report_dir, retention_days)
    cleanup_old_reports(report_dir, report_retention)

    schedule.every(interval).minutes.do(
        capture_screenshot, output_dir, privacy_config, idle_skip, capture_all
    )
    schedule.every(1).hours.do(cleanup_old_screenshots, output_dir, report_dir, retention_days)
    schedule.every(1).hours.do(cleanup_old_reports, report_dir, report_retention)
    schedule.every(1).hours.do(run_hourly_analysis)

    print(
        f"Capture started (interval={interval}min, retention={retention_days}d, "
        f"idle_skip={int(idle_skip)}s, displays={'all' if capture_all else 'active'}, "
        f"accessibility={'on' if accessibility.is_trusted() else 'NO PERMISSION'}). "
        f"PID={os.getpid()}"
    )
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
