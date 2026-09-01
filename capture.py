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


def get_frontmost_window_title() -> str:
    """获取当前前台窗口的标题（macOS）"""
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to get name of first window of (first process whose frontmost is true)',
            ],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def get_frontmost_app_name() -> str:
    """获取当前前台应用名称（macOS）"""
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to get name of first process whose frontmost is true',
            ],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
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


def capture_screenshot(
    output_dir: Path,
    privacy_config: dict | None = None,
    idle_skip_seconds: float = 300.0,
):
    now = datetime.now()

    idle = get_idle_seconds()
    if idle_skip_seconds and idle >= idle_skip_seconds:
        print(f"[{now.strftime('%H:%M:%S')}] Skipped: idle {int(idle)}s")
        return

    app_name = get_frontmost_app_name()
    title = get_frontmost_window_title()

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

    subprocess.run(
        ["screencapture", "-x", "-t", "png", str(filepath)],
        check=True,
        capture_output=True,
    )

    # 同时保存上下文元数据（应用名+窗口标题），供后续代码分析用
    meta_file = date_dir / f"{filename}.meta"
    meta_file.write_text(f"{app_name}\n{title}")

    # 无障碍文本必须在截图的同一时刻抓 —— 界面变了就再也读不到了
    ax_chars = 0
    try:
        pid, _ = accessibility.frontmost_app()
        ax_text, _ = accessibility.capture_screen_text(pid)
        if ax_text:
            (date_dir / f"{filename}.txt").write_text(ax_text)
            ax_chars = len(ax_text)
    except Exception as e:
        print(f"[{now.strftime('%H:%M:%S')}] accessibility failed: {e}")

    print(f"[{now.strftime('%H:%M:%S')}] {app_name} | {title[:40]} | ax={ax_chars}字")


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
    config = load_config()
    capture_config = config["capture"]
    interval = capture_config["interval_minutes"]
    output_dir = SCRIPT_DIR / capture_config["output_dir"]
    retention_days = capture_config["retention_days"]
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    privacy_config = config.get("privacy")
    idle_skip = float(capture_config.get("idle_skip_seconds", 300))

    output_dir.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    write_pid()

    capture_screenshot(output_dir, privacy_config, idle_skip)
    cleanup_old_screenshots(output_dir, report_dir, retention_days)

    schedule.every(interval).minutes.do(capture_screenshot, output_dir, privacy_config, idle_skip)
    schedule.every(1).hours.do(cleanup_old_screenshots, output_dir, report_dir, retention_days)
    schedule.every(1).hours.do(run_hourly_analysis)

    print(
        f"Capture started (interval={interval}min, retention={retention_days}d, "
        f"idle_skip={int(idle_skip)}s, accessibility={'on' if accessibility.is_trusted() else 'NO PERMISSION'}). "
        f"PID={os.getpid()}"
    )
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
