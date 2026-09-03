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
from PIL import Image

import accessibility
import audit

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


def _idle(event_type) -> float:
    try:
        import Quartz
        # 1 = kCGEventSourceStateCombinedSessionState，即整个登录会话
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(1, event_type))
    except Exception:
        return 0.0


def get_idle_seconds() -> float:
    """系统空闲了多久。锁屏、离开工位时截图没有任何信息量，只是烧钱。

    原来 fork 一个 ioreg 子进程读 HIDIdleTime，22 毫秒。
    改成 Quartz 直接问，0 毫秒且读数一致（实测 4.2/4.4、2.4/2.6）——
    事件驱动要每秒轮询，22 毫秒的开销撑不住。
    """
    try:
        import Quartz
        return _idle(Quartz.kCGAnyInputEventType)
    except Exception:
        return 0.0


def keyboard_idle_seconds() -> float:
    """离上次按键过了多久。用来识别「打字停顿」——
    一段话写完、一条命令敲完的那一刻，屏幕上的内容最完整。"""
    try:
        import Quartz
        return _idle(Quartz.kCGEventKeyDown)
    except Exception:
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
    reason: str = "",
    image_config: dict | None = None,
):
    now = datetime.now()

    left = paused_seconds()
    if left:
        print(f"[{now.strftime('%H:%M:%S')}] Skipped: paused ({left//60}分{left%60}秒后恢复)")
        return

    # 变量名不能叫 reason —— 那是入参（触发原因），会被覆盖成空串
    blocked = screen_unavailable()
    if blocked:
        print(f"[{now.strftime('%H:%M:%S')}] Skipped: {blocked}")
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

    fmt = (image_config or {}).get("format", "jpeg")
    max_width = (image_config or {}).get("max_width", 1600)
    quality = (image_config or {}).get("quality", 85)
    ext = "jpg" if fmt == "jpeg" else "png"

    filename = now.strftime("%H-%M-%S")
    filepath = date_dir / f"{filename}.{ext}"

    displays = list_displays()
    active = pick_display_index(pid)

    def shoot(target: int | None, path: Path):
        """截图并按配置压缩落盘。

        全分辨率无损 PNG 一张 1.46 MB，双屏一天就是 460 MB ——
        而这张图只有两个用途：OCR 兜底、人工翻看，都不需要无损。
        实测 1600px JPEG q85 只占 5%，OCR 文字保留 98%（801/815 字）。
        """
        raw = path.with_suffix(".rawpng") if fmt == "jpeg" else path
        cmd = ["screencapture", "-x", "-t", "png"]
        if target is not None:
            cmd += ["-D", str(target)]
        cmd.append(str(raw))
        subprocess.run(cmd, check=True, capture_output=True)

        if fmt != "jpeg":
            return
        try:
            img = Image.open(raw).convert("RGB")
            if img.size[0] > max_width:
                h = int(img.size[1] * max_width / img.size[0])
                img = img.resize((max_width, h), Image.LANCZOS)
            img.save(path, "JPEG", quality=quality, optimize=True)
        finally:
            raw.unlink(missing_ok=True)

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
            side = date_dir / f"{filename}-s{idx}.{ext}"
            try:
                shoot(idx, side)
            except subprocess.CalledProcessError:
                continue
            side_app = app_on_display(bounds)
            # 三行格式要和主文件一致：应用名 / 标题 / pid。
            # 应用名为空时写「未知应用」而不是留空 —— 留空会让 read_meta
            # 把第三行的 "pid=" 当成应用名，混进时间统计
            side.with_suffix(".meta").write_text(f"{side_app or '未知应用'}\n\npid=")
            extra += 1

    screen = f" | 屏{active}" if active is not None else ""
    more = f" +{extra}屏" if extra else ""
    why = f" | {reason}" if reason else ""
    print(f"[{now.strftime('%H:%M:%S')}] {app_name} | {title[:40]} | ax={ax_chars}字{screen}{more}{why}")


IMAGE_SUFFIXES = (".png", ".jpg")
# 文本类产物：无障碍树、OCR 缓存、元数据。这些才是「记忆」本身。
TEXT_SUFFIXES = (".txt", ".ocr", ".meta")


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# 无障碍文本少于这个字数就当没抽到实质内容，需要 OCR 补。
# 和 analyze.py 的 _AX_TRUST_CHARS 是同一条线：终端类应用每帧稳定给
# 一百多字的标签栏，字数够但全是 UI 装饰。
_AX_TRUST_CHARS = 1000


def needs_ocr(image: Path) -> bool:
    """这张图是否还没被抽成足够的文本。"""
    if image.with_suffix(".ocr").exists():
        return False
    ax = image.with_suffix(".txt")
    if not ax.exists():
        return True
    try:
        return len(ax.read_text().strip()) < _AX_TRUST_CHARS
    except OSError:
        return True


def backfill_ocr(output_dir: Path, limit: int = 40) -> int:
    """把还没有文本的图片补成文本，从旧到新。

    为什么需要它：.ocr 原本只在分析时按需生成，而分析前要去重、
    还封顶 30 帧 —— 被筛掉的帧从来没被 OCR 过。配上图片保留期，
    这部分画面的内容就永久消失了。实测 162 张里有 49 张属于这种情况。

    放在后台定时任务里而不是采集主循环里：OCR 一张约 1 秒，
    塞进采集会拖慢前台响应，而它并不需要实时完成 ——
    只要赶在图片被删之前做完就行。
    每轮有上限，避免积压时一次占住 CPU 几分钟。
    """
    if not output_dir.exists():
        return 0
    import ocr as ocr_mod

    pending = []
    for date_dir in sorted(output_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for f in sorted(date_dir.iterdir()):
            if f.suffix in IMAGE_SUFFIXES and needs_ocr(f):
                pending.append(f)

    done = 0
    for image in pending[:limit]:
        try:
            text = "\n".join(ocr_mod.recognize(str(image)))
            image.with_suffix(".ocr").write_text(text)
            done += 1
        except Exception as e:
            print(f"OCR 失败 {image.name}: {e}")
    if done:
        print(f"已补 OCR {done} 张（还欠 {max(0, len(pending) - done)} 张）")
    return done


def drop_images(output_dir: Path, keep_days: int) -> int:
    """删掉超过 keep_days 的图片，保留同名的文本。

    实测这批数据里图片占 99.9%，文本占 0.1% —— 文本是图片的 1/841。
    图片一旦被抽成文本，剩下的用途只有人工翻看；
    而「上个月某天我在干什么」这种问题，文本答得了，图片答不了更多。
    磁盘紧张时先砍图片，是唯一不牺牲记忆能力的砍法。
    0 表示不删。
    """
    if not keep_days or not output_dir.exists():
        return 0
    # 目录名是日期，按当天 23:59 算而不是 00:00 —— 否则 keep_days=2 时
    # 前天的图片今天白天就被删了，实际只留了一天多
    cutoff = datetime.now() - timedelta(days=keep_days)
    freed = rescued = removed = 0
    for date_dir in sorted(output_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            day_end = datetime.strptime(date_dir.name, "%Y-%m-%d") + timedelta(days=1)
            if day_end >= cutoff:
                continue
        except ValueError:
            continue
        for f in sorted(date_dir.iterdir()):
            if f.suffix not in IMAGE_SUFFIXES:
                continue
            # 删之前最后一道保险：这张图还没被抽成文本的话，现在抽。
            # 少了这一步，「删图片保留文本」就成了「删图片顺便丢内容」
            if needs_ocr(f):
                try:
                    import ocr as ocr_mod
                    f.with_suffix(".ocr").write_text(
                        "\n".join(ocr_mod.recognize(str(f)))
                    )
                    rescued += 1
                except Exception as e:
                    print(f"删前 OCR 失败，保留原图 {f.name}: {e}")
                    continue
            freed += f.stat().st_size
            f.unlink()
            removed += 1
    if freed:
        extra = f"，删前补抽 {rescued} 张" if rescued else ""
        print(f"已清理旧图片，释放 {freed / 1024 / 1024:.0f} MB（文本保留{extra}）")
        audit.record("定时任务", f"图片超过保留期 {keep_days} 天", removed, freed)
    return freed


def enforce_disk_budget(output_dir: Path, max_mb: int) -> int:
    """把占用压到上限以内。先删最旧的图片，还不够才删最旧的文本。

    有这个才敢在小硬盘的机器上装：不管跑多久，占用不会越过这条线。
    保留期是「多久之前的删掉」，这个是「最多用这么多」—— 后者才是
    磁盘紧张时真正需要的保证。
    0 表示不限制。
    """
    if not max_mb or not output_dir.exists():
        return 0
    limit = max_mb * 1024 * 1024
    used = _dir_bytes(output_dir)
    if used <= limit:
        return 0

    # 先图片后文本，各自从旧到新
    images, texts = [], []
    for f in output_dir.rglob("*"):
        if not f.is_file():
            continue
        (images if f.suffix in IMAGE_SUFFIXES else texts).append(f)
    images.sort(key=lambda f: f.name)
    images.sort(key=lambda f: f.parent.name)
    texts.sort(key=lambda f: f.name)
    texts.sort(key=lambda f: f.parent.name)

    freed = 0
    for f in images + texts:
        if used - freed <= limit:
            break
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass

    # 清掉空掉的日期目录
    for d in output_dir.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    print(f"超出磁盘上限 {max_mb} MB，已释放 {freed / 1024 / 1024:.0f} MB")
    audit.record("定时任务", f"超出磁盘上限 {max_mb}MB", 0, freed)
    return freed


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
        audit.record("定时任务", f"报告超过保留期 {retention_days} 天：{date_dir.name}")


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


def pending_hours(
    output_dir: Path, report_dir: Path, lookback_days: int = 3, cap: int = 12
) -> list[tuple[str, int]]:
    """有截图却还没有报告的小时。

    为什么不能只靠定时器：schedule.every(1).hours 是从进程启动开始计时的，
    重启一次就归零。实测反复重启服务之后，昨天 19 点那 14 张截图
    永远等不到分析 —— 睡眠、崩溃、升级都会留下同样的永久空洞。
    改成每次都去对账「谁有数据但没报告」，任何原因造成的漏都能补上。

    跳过当前小时（还在攒），并设上限防止积压时一次性烧掉一大笔。
    """
    now = datetime.now()
    current = (now.strftime("%Y-%m-%d"), now.hour)
    cutoff = now - timedelta(days=lookback_days)
    found: list[tuple[str, int]] = []

    if not output_dir.exists():
        return found
    for date_dir in sorted(output_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            if datetime.strptime(date_dir.name, "%Y-%m-%d") < cutoff:
                continue
        except ValueError:
            continue
        hours = {f.name[:2] for f in date_dir.iterdir()
                 if f.suffix in (".png", ".jpg")}
        for hh in sorted(hours):
            if not hh.isdigit():
                continue
            key = (date_dir.name, int(hh))
            if key == current:
                continue
            day_reports = report_dir / date_dir.name
            if (day_reports / f"{hh}.md").exists():
                continue
            # 分析过、但确实不该有报告（锁屏、全是仅本地应用）——
            # 不认这个标记就会每小时重试一次，永远重试下去
            if (day_reports / f"{hh}.skipped").exists():
                continue
            found.append(key)
    return found[-cap:]


def analyze_hour(date_str: str, hour: int) -> bool:
    python = str(SCRIPT_DIR / "venv" / "bin" / "python")
    stamp = datetime.now().strftime("%H:%M:%S")
    try:
        result = subprocess.run(
            [python, str(SCRIPT_DIR / "analyze.py"), "--date", date_str, "--hour", str(hour)],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        print(f"[{stamp}] 分析 {date_str} {hour:02d} 出错: {e}")
        return False
    if result.returncode == 0:
        print(f"[{stamp}] 已分析 {date_str} {hour:02d}:00")
        return True
    # analyze.py 把原因打在 stdout，只看 stderr 会得到空字符串 ——
    # 之前日志里那一串「Auto analysis failed:」后面什么都没有就是这么来的
    detail = (result.stderr.strip() or result.stdout.strip() or "无输出").splitlines()[-1]
    print(f"[{stamp}] 分析 {date_str} {hour:02d} 未完成: {detail[:120]}")
    return False


def pending_days(
    output_dir: Path, report_dir: Path, lookback_days: int = 7,
    include_today: bool = False,
) -> list[str]:
    """有截图却还没有日总结的日期。

    默认不含今天（还没过完）。但定时任务在 23:50 触发时「今天」已经
    实质结束了 —— 不带 include_today 的话，当天的总结永远轮不到：
    23:50 那次因为是今天而跳过，第二天那次才补上，等于永远晚一天。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = datetime.now() - timedelta(days=lookback_days)
    out = []
    if not output_dir.exists():
        return out
    for date_dir in sorted(output_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        if date_dir.name == today and not include_today:
            continue
        try:
            if datetime.strptime(date_dir.name, "%Y-%m-%d") < cutoff:
                continue
        except ValueError:
            continue
        if not any(f.suffix in IMAGE_SUFFIXES for f in date_dir.iterdir()):
            continue
        if (report_dir / date_dir.name / "daily-summary.md").exists():
            continue
        if (report_dir / date_dir.name / "daily.skipped").exists():
            continue
        out.append(date_dir.name)
    return out


def summarize_day(date_str: str) -> bool:
    python = str(SCRIPT_DIR / "venv" / "bin" / "python")
    stamp = datetime.now().strftime("%H:%M:%S")
    try:
        r = subprocess.run(
            [python, str(SCRIPT_DIR / "summarize.py"), "--date", date_str],
            capture_output=True, text=True, timeout=900,
        )
    except Exception as e:
        print(f"[{stamp}] 日总结 {date_str} 出错: {e}")
        return False
    if r.returncode == 0:
        print(f"[{stamp}] 日总结完成 {date_str}")
        return True
    detail = (r.stderr.strip() or r.stdout.strip() or "无输出").splitlines()[-1]
    print(f"[{stamp}] 日总结 {date_str} 未完成: {detail[:120]}")
    return False


def run_daily_summary(output_dir: Path, report_dir: Path, scheduled: bool = False):
    """补齐所有欠着的日总结。

    小时报告不再自动分析 —— 那是每小时一次模型调用，而绝大多数小时
    根本不会有人去看。改成用户在看板上点某个小时才分析。
    日总结每天一次：它直接从屏幕文本出发，已有的小时报告顺带复用，
    所以一天只花一次调用，不是十几次。

    补齐而不是「只做昨天」：机器在定点时刻可能是关着的，
    只做昨天会留下永久空洞。
    """
    # 定时触发且已过 20 点：把今天也算进去 —— 那时候这一天该发生的
    # 基本都发生完了。启动时的补齐调用不带这个，免得大清早给半天数据出总结。
    include_today = scheduled and datetime.now().hour >= 20
    todo = pending_days(output_dir, report_dir, include_today=include_today)
    if not todo:
        return
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 待生成日总结 {len(todo)} 天: {', '.join(todo)}")
    for date_str in todo:
        summarize_day(date_str)


def capture_reason(
    now: float,
    last_capture: float,
    sig: tuple,
    last_sig: tuple | None,
    kb_idle: float,
    prev_kb_idle: float,
    cfg: dict,
) -> str | None:
    """该不该在此刻采集，以及为什么。返回 None 表示不采。

    固定间隔的毛病是与实际活动无关：盯着一篇文章看 30 分钟，
    拍出 10 张一模一样的；两分钟里切了 5 个应用，只拍到 1 个。
    改成由事件触发，同样的张数能覆盖更多真正发生过的事。
    """
    since = now - last_capture

    # 下限：切换风暴（alt-tab 连按）时不要每次都拍
    if since < cfg["min_interval"]:
        return None

    if last_sig is not None and sig != last_sig:
        return "切换"

    # 打字停顿：一段话写完、一条命令敲完的那一刻，屏幕内容最完整。
    # 用「上一拍还在打字、这一拍停了」的跳变，而不是「现在没在打字」——
    # 后者在整个休息期间会一直为真。
    #
    # 它的节流阈值比切换高得多：切换意味着换了上下文，是强信号；
    # 同一个窗口里的打字停顿只说明多了几行字，是弱信号。
    # 实测在钉钉里聊天时每 30 秒就停顿一次，共用 20 秒阈值会让
    # 采集频率比固定间隔还高 5 倍 —— 事件驱动本来是为了少拍而拍得更准。
    pause = cfg["typing_pause"]
    if prev_kb_idle < pause <= kb_idle and since >= cfg["pause_interval"]:
        return "停顿"

    # 上限：一直没事件也要留个记录，否则长时间阅读会整段留白
    if since >= cfg["max_interval"]:
        return "定时"

    return None


def event_loop(output_dir: Path, report_dir: Path, cfg: dict, capture_config: dict,
               privacy_config: dict | None, capture_all: bool):
    """事件驱动主循环。

    每秒轮询的四个信号加起来 0.24 毫秒（应用名 0.00、窗口标题 0.04、
    输入空闲 0.00、锁屏 0.20），全天跑也可以忽略不计。
    无障碍全树是 70 毫秒，所以只在真正采集时才走。
    """
    # 从「现在」起算，而不是 0 —— 否则第一轮 since 是个天文数字，
    # 立刻判定超上限，启动那一张刚拍完就又拍一张
    last_capture = time.time()
    last_sig: tuple | None = None
    prev_kb_idle = keyboard_idle_seconds()
    idle_skip = cfg["idle_skip"]

    while True:
        time.sleep(cfg["poll"])
        schedule.run_pending()

        if paused_seconds() or screen_unavailable():
            last_sig = None  # 恢复后第一帧当作切换
            continue
        if idle_skip and get_idle_seconds() >= idle_skip:
            continue

        pid, app_name, title = frontmost()
        sig = (app_name, title)
        kb_idle = keyboard_idle_seconds()
        now = time.time()

        reason = capture_reason(
            now, last_capture, sig, last_sig, kb_idle, prev_kb_idle, cfg
        )
        prev_kb_idle = kb_idle

        if last_sig is None:
            last_sig = sig
            if reason is None:
                continue
        last_sig = sig

        if reason is None:
            continue

        # 切换之后界面还在渲染，立刻截会拍到半张空白
        if reason == "切换":
            time.sleep(cfg["settle"])

        capture_screenshot(output_dir, privacy_config, idle_skip, capture_all, reason,
                           cfg["image"])
        last_capture = time.time()


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

    mode = capture_config.get("mode", "event")

    image_config = capture_config.get("image", {})
    capture_screenshot(output_dir, privacy_config, idle_skip, capture_all, "启动", image_config)
    image_days = capture_config.get("image_retention_days", 0)
    max_disk_mb = capture_config.get("max_disk_mb", 0)

    def housekeeping():
        # 顺序有讲究：先把欠的文本补上，再删图片。反过来就是丢内容。
        backfill_ocr(output_dir, capture_config.get("ocr_backfill_per_hour", 40))
        cleanup_old_screenshots(output_dir, report_dir, retention_days)
        drop_images(output_dir, image_days)
        enforce_disk_budget(output_dir, max_disk_mb)
        cleanup_old_reports(report_dir, report_retention)

    housekeeping()
    run_daily_summary(output_dir, report_dir)

    if mode == "interval":
        schedule.every(interval).minutes.do(
            capture_screenshot, output_dir, privacy_config, idle_skip, capture_all,
            "定时", image_config
        )
    schedule.every(1).hours.do(housekeeping)
    # 日总结每天一次。按钟点触发而不是「每隔 24 小时」：
    # 后者从进程启动计时，重启一次就漂移。
    daily_at = config["report"].get("daily_summary_at", "23:50")
    schedule.every().day.at(daily_at).do(
        run_daily_summary, output_dir, report_dir, True
    )

    pace = (
        f"interval={interval}min" if mode == "interval"
        else f"event(min={capture_config.get('min_interval_seconds', 20)}s/"
             f"max={capture_config.get('max_interval_seconds', 300)}s)"
    )
    print(
        f"Capture started ({pace}, retention={retention_days}d, "
        f"idle_skip={int(idle_skip)}s, displays={'all' if capture_all else 'active'}, "
        f"daily@{daily_at}, "
        f"accessibility={'on' if accessibility.is_trusted() else 'NO PERMISSION'}). "
        f"PID={os.getpid()}"
    )

    if mode == "interval":
        while True:
            schedule.run_pending()
            time.sleep(1)
    else:
        event_loop(
            output_dir, report_dir,
            {
                "min_interval": capture_config.get("min_interval_seconds", 20),
                "max_interval": capture_config.get("max_interval_seconds", 300),
                "poll": capture_config.get("poll_seconds", 1),
                "settle": capture_config.get("settle_seconds", 0.6),
                "typing_pause": capture_config.get("typing_pause_seconds", 3),
                "pause_interval": capture_config.get("pause_min_interval_seconds", 90),
                "idle_skip": idle_skip,
                "image": image_config,
            },
            capture_config, privacy_config, capture_all,
        )


if __name__ == "__main__":
    main()
