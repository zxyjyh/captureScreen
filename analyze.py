"""小时分析脚本 - 代码提取结构化信息 + AI 只理解图片内容"""

import argparse
import base64
import io
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from PIL import Image
import llm

import ocr

SCRIPT_DIR = Path(__file__).resolve().parent

# launchd / cron 启动时不继承 shell 环境，凭证只能从 .env 取
import env_file  # noqa: E402

env_file.load()

_DIFF_THRESHOLD = 50.0
_COMPARE_SIZE = (160, 100)
_MAX_IMAGE_WIDTH = 1280
_JPEG_QUALITY = 75
# 少于这个字数说明无障碍树没抓到实质内容（多半只有窗口标题），该退回 OCR
_MIN_USEFUL_CHARS = 80
# 无障碍树字数超过这个就直接信任，不必再跑 OCR。
# 之所以要这道门槛：光看「有没有超过 80 字」会被 UI 装饰骗过 ——
# 实测 Ghostty 每帧稳定给 203 字，全是「Close tab」「⌘1」这类标签栏文本，
# 终端里真正的内容一个字都拿不到，而同一张图 OCR 能出 400 字真内容。
# 内容真的丰富的应用（Chrome 一屏 5000+ 字）远在这条线之上。
_AX_TRUST_CHARS = 1000
# 灰度方差低于这个值就当作没有画面。锁屏纯黑接近 0，
# 正常界面（哪怕是深色主题）都在几百以上
_BLANK_VARIANCE = 15.0
# 历史数据是 PNG，新数据是 JPEG，两种都要认
IMAGE_SUFFIXES = {".png", ".jpg"}


# ==================== 代码能做的事（确定性） ====================

def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def get_screenshots(screenshot_dir: Path, date_str: str, hour: int) -> list[Path]:
    date_dir = screenshot_dir / date_str
    if not date_dir.exists():
        return []
    hour_prefix = f"{hour:02d}-"
    return sorted(
        f for f in date_dir.iterdir()
        if f.is_file() and f.name.startswith(hour_prefix) and f.suffix in IMAGE_SUFFIXES
    )


def cap_frames(screenshots: list[Path], limit: int) -> list[Path]:
    """把一小时的帧数压到上限以内，均匀抽样。

    事件驱动加多屏之后，活跃的一小时可能攒出 50+ 帧 ——
    去重挡不住（不同屏、不同窗口本来就长得不一样），
    但一小时的分析成本不该跟着无限涨。
    均匀抽样而不是取前 N 张：后者会让整点后的活动完全看不见。
    首尾必留，保住这一小时的起止。
    """
    if limit <= 0 or len(screenshots) <= limit:
        return screenshots
    step = len(screenshots) / limit
    picked = [screenshots[int(i * step)] for i in range(limit)]
    if picked[-1] != screenshots[-1]:
        picked[-1] = screenshots[-1]
    print(f"帧数封顶: {len(screenshots)} -> {len(picked)}")
    return picked


def display_of(screenshot: Path) -> str:
    """这一帧属于哪块屏。副屏文件名形如 09-34-46-s2.jpg。

    无后缀的那张是「前台窗口所在的屏」，它的实际编号会变 ——
    所以返回 active 而不是 s1：前台在屏 2 时副屏正好叫 -s1，
    两者会撞成同一组，去重就把不同屏的画面当成同一块屏比较了。
    """
    parts = screenshot.stem.split("-")
    return parts[3] if len(parts) > 3 else "active"


def deduplicate_screenshots(screenshots: list[Path], threshold: float = _DIFF_THRESHOLD) -> list[Path]:
    """按屏分组去重。

    多屏采集下相邻两帧常来自不同显示器，画面必然不同 ——
    不分组的话去重完全失效，一张都删不掉。
    """
    groups: dict[str, list[Path]] = {}
    for ss in screenshots:
        groups.setdefault(display_of(ss), []).append(ss)
    if len(groups) > 1:
        kept = [f for g in groups.values() for f in _dedup_one(g, threshold)]
        print(f"Dedup: {len(screenshots)} -> {len(kept)} ({len(groups)} 块屏分别去重)")
        return sorted(kept)
    kept = _dedup_one(screenshots, threshold)
    print(f"Dedup: {len(screenshots)} -> {len(kept)}")
    return kept


def _dedup_one(screenshots: list[Path], threshold: float) -> list[Path]:
    if len(screenshots) <= 2:
        return screenshots
    kept = [screenshots[0]]
    prev_img = Image.open(screenshots[0]).resize(_COMPARE_SIZE).convert("L")
    for ss in screenshots[1:]:
        curr_img = Image.open(ss).resize(_COMPARE_SIZE).convert("L")
        if _mse(prev_img, curr_img) > threshold:
            kept.append(ss)
            prev_img = curr_img
    if kept[-1] != screenshots[-1]:
        kept.append(screenshots[-1])
    return kept


def _mse(img1, img2) -> float:
    p1, p2 = list(img1.getdata()), list(img2.getdata())
    return sum((a - b) ** 2 for a, b in zip(p1, p2)) / len(p1)


def extract_time_from_filename(filepath: Path) -> str:
    """11-51-00.png -> 11:51"""
    parts = filepath.stem.split("-")
    return f"{parts[0]}:{parts[1]}"


def read_meta(filepath: Path) -> dict:
    """读取截图时保存的元数据（应用名 + 窗口标题）"""
    meta_file = filepath.with_suffix(".meta")
    if meta_file.exists():
        # 用 splitlines 而不是 split("\n", 1)：meta 第三行是 pid=，
        # 限制切分次数会把它并进标题
        lines = meta_file.read_text().strip().splitlines()
        return {"app": lines[0] if lines else "", "title": lines[1] if len(lines) > 1 else ""}
    return {"app": "", "title": ""}


def build_timeline(screenshots: list[Path]) -> list[dict]:
    """代码生成时间线：从元数据文件读取应用名和窗口标题"""
    timeline = []
    prev_app = ""
    segment_start = ""

    for ss in screenshots:
        time_str = extract_time_from_filename(ss)
        meta = read_meta(ss)
        app = meta["app"] or "未知应用"
        title = meta["title"]

        if app != prev_app:
            if prev_app:
                timeline.append({"from": segment_start, "to": time_str, "app": prev_app, "title": ""})
            segment_start = time_str
            prev_app = app

    if prev_app and segment_start:
        timeline.append({
            "from": segment_start,
            "to": extract_time_from_filename(screenshots[-1]),
            "app": prev_app,
            "title": read_meta(screenshots[-1]).get("title", ""),
        })

    return timeline


def build_time_allocation(timeline: list[dict]) -> dict[str, int]:
    """代码统计每个应用的使用分钟数"""
    allocation = {}
    for entry in timeline:
        start = datetime.strptime(entry["from"], "%H:%M")
        end = datetime.strptime(entry["to"], "%H:%M")
        minutes = max(1, int((end - start).total_seconds() / 60))
        app = entry["app"]
        allocation[app] = allocation.get(app, 0) + minutes
    return dict(sorted(allocation.items(), key=lambda x: -x[1]))


def format_timeline(timeline: list[dict]) -> str:
    lines = []
    for entry in timeline:
        title = f" - {entry.get('title', '')}" if entry.get("title") else ""
        lines.append(f"- {entry['from']}-{entry['to']} {entry['app']}{title}")
    return "\n".join(lines)


def format_allocation(allocation: dict[str, int]) -> str:
    total = sum(allocation.values())
    lines = []
    for app, minutes in allocation.items():
        pct = minutes / total * 100 if total else 0
        lines.append(f"- {app}: {minutes}分钟 ({pct:.0f}%)")
    return "\n".join(lines)


# ==================== AI 只做这件事：理解图片内容 ====================

def frame_text(screenshot: Path) -> tuple[str, str]:
    """取这一帧的屏幕文本，返回 (文本, 来源)。

    两个本地来源，都不花钱：
      .txt   采集时抓的无障碍树 —— 结构化、准确，但有些应用（终端、
             Electron 的部分实现）只暴露窗口边框，内容一个字不给
      .ocr   Apple Vision 本地 OCR —— 全覆盖，但会把图标误读成碎字

    因为 OCR 本地免费，所以不做成「无障碍树不够就退回 OCR」的单向兜底：
    无障碍树不够丰富时两个都取，谁给的多用谁。用字数当信息量的代理指标
    是粗糙的，但它便宜、可解释，而且实测能正确区分「203 字标签栏」和
    「400 字真内容」这种情况。

    OCR 结果落盘缓存：重跑分析时不必再算一遍，一张图 1 秒不算便宜。
    """
    ax_text = ""
    ax_file = screenshot.with_suffix(".txt")
    if ax_file.exists():
        ax_text = ax_file.read_text().strip()
        if len(ax_text) >= _AX_TRUST_CHARS:
            return ax_text, "accessibility"

    cache = screenshot.with_suffix(".ocr")
    if cache.exists():
        ocr_text, ocr_source = cache.read_text().strip(), "ocr-cached"
    else:
        try:
            ocr_text = "\n".join(ocr.recognize(str(screenshot)))
            cache.write_text(ocr_text)
            ocr_source = "ocr"
        except Exception as e:
            print(f"OCR failed on {screenshot.name}: {e}")
            ocr_text, ocr_source = "", "none"

    if len(ax_text) >= len(ocr_text) and len(ax_text) >= _MIN_USEFUL_CHARS:
        return ax_text, "accessibility"
    if len(ocr_text) >= _MIN_USEFUL_CHARS:
        return ocr_text, ocr_source
    return "", "none"


def looks_blank(screenshot: Path) -> bool:
    """这一帧是不是几乎没有画面（锁屏、黑屏、纯色）。

    用途是区分两种「抽不到文字」：屏幕上本来就什么都没有，
    还是有画面但都是图（视频、设计稿）。前者该直接跳过，
    后者才值得退回多模态。
    分不清的话会把黑屏送进视觉模型，模型就对着黑屏编内容 ——
    实测编出过「用户可能处于任务准备阶段」这种整段虚构。
    """
    try:
        img = Image.open(screenshot).convert("L").resize((64, 40))
        px = list(img.getdata())
        mean = sum(px) / len(px)
        var = sum((v - mean) ** 2 for v in px) / len(px)
        return var < _BLANK_VARIANCE
    except Exception:
        return False


def is_local_only(app: str, local_only_apps: list[str]) -> bool:
    """这个应用的内容是否只许留在本机、不许发给模型。"""
    low = (app or "").lower()
    return any(k.lower() in low for k in local_only_apps)


def build_text_context(
    screenshots: list[Path], local_only_apps: list[str] | None = None
) -> tuple[str, dict[str, int]]:
    """把关键帧拼成给模型的文本上下文，并统计各来源占比。

    local_only_apps 里的应用会被整帧排除 —— 文件照常留在本地、
    search_screen 照样搜得到（那些工具不联网），只是不进入出网的上下文。
    控制点放在这里而不是采集端：数据一直在本机不构成泄漏，
    真正的风险是把公司内部内容发给第三方模型。
    """
    local_only_apps = local_only_apps or []
    blocks: list[str] = []
    stats: dict[str, int] = {}
    for ss in screenshots:
        meta = read_meta(ss)
        if is_local_only(meta.get("app", ""), local_only_apps):
            stats["local-only"] = stats.get("local-only", 0) + 1
            continue
        text, source = frame_text(ss)
        stats[source] = stats.get(source, 0) + 1
        if not text:
            continue
        # 标出副屏，否则同一时刻两块屏的内容会被当成用户先后做的两件事
        d = display_of(ss)
        where = "" if d == "active" else f" [副屏{d[1:]}]"
        header = (f"[{extract_time_from_filename(ss)}]{where} "
                  f"{meta.get('app', '')} | {meta.get('title', '')}")
        blocks.append(f"{header}\n{text}")
    return "\n\n---\n\n".join(blocks), stats


def allowed_times(screenshots: list[Path]) -> str:
    """把这批帧的时间显式列出来。

    只在 prompt 里讲「不要用屏幕上的时间」不够 —— 模型照样会把
    聊天消息的发送时间写成步骤时间。给一份白名单更管用。
    """
    return "、".join(extract_time_from_filename(s) for s in screenshots)


AI_TEXT_PROMPT = """你是一个个人工作记录员。下面是用户这段时间的屏幕文本，
按时间顺序排列，每块以 [时间] 应用 | 窗口标题 开头，正文是该时刻屏幕上的文字。

这些文字来自无障碍树或 OCR，会有噪音（图标误读、菜单项、快捷键）。忽略噪音，
抓实质内容。

按以下结构输出：

## 活动流

按时间还原用户做了什么，每步写明：时间、应用、具体在做什么。
步骤之间如果有因果关系（因为遇到 X 所以去查 Y），写出来。

**时间只能从下面这份清单里选**，一个都不许多出来：
{allowed}

屏幕正文里出现的时间（聊天消息的发送时间、日志时间戳、日历上的时间）
是被观察到的内容，不是用户在做这件事的时间，绝不能拿来当步骤时间。

## 关键事实

抽取可被检索的具体信息，有就写，没有就跳过该项：

- **实体**：涉及的项目名、仓库名、产品名、人名、公司名
- **文件与路径**：出现过的文件名、目录路径
- **链接**：出现过的 URL 或站点
- **报错与问题**：具体的错误码、错误信息、卡住的地方
- **决定与结论**：做了什么判断、选了哪个方案
- **待办**：明确提到但还没做完的事

## 阅读与信息摄入

如果有文章、文档、网页正文：列出标题与来源，总结核心观点。
同一篇文章滚动产生的多段，拼成一篇再总结，不要重复。

要求：
- 只写屏幕上真实出现过的内容，**不要推测、不要补全、不要发挥**
- 不做效率点评、不给改进建议、不做时间统计 —— 这些没有检索价值
- 具体优先于概括：写「修 40164 IP 白名单报错」，不写「处理了技术问题」
"""


AI_PROMPT = """你是一个个人工作记录员。下面是用户这段时间的屏幕截图，按时间顺序排列。

这条路径只在无障碍树和 OCR 都抽不到文字时才会走到，说明屏幕上主要是图像内容。

按以下结构输出：

## 活动流

按时间还原用户做了什么，每步写明：时间（从截图顺序推断）、应用、具体在做什么。
步骤之间如果有因果关系，写出来。

**截图里出现的时间（聊天消息时间、日志时间戳）是被观察到的内容，
不是用户在做这件事的时间，不要拿来当步骤时间。**

## 关键事实

抽取可被检索的具体信息，有就写，没有就跳过该项：

- **实体**：涉及的项目名、仓库名、产品名、人名、公司名
- **文件与路径**：出现过的文件名、目录路径
- **链接**：出现过的 URL 或站点
- **报错与问题**：具体的错误码、错误信息、卡住的地方
- **决定与结论**：做了什么判断、选了哪个方案
- **待办**：明确提到但还没做完的事

## 阅读与信息摄入

如果有文章、文档、网页正文：列出标题与来源，总结核心观点。
同一篇内容滚动产生的多张，拼成一篇再总结，不要重复。

要求：
- **只写截图里真实看得见的内容。看不清就说看不清，不要推测、不要补全、不要发挥**
- 不做效率点评、不给改进建议、不做时间统计、不推测用户心态 —— 这些没有检索价值，
  且极易变成虚构
- 具体优先于概括：写「修 40164 IP 白名单报错」，不写「处理了技术问题」
"""


def encode_image(filepath: Path) -> str:
    img = Image.open(filepath).convert("RGB")
    if img.size[0] > _MAX_IMAGE_WIDTH:
        ratio = _MAX_IMAGE_WIDTH / img.size[0]
        img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _client():
    """按 config 里的 provider 取客户端。两个后端调用形状一致。"""
    cfg = load_config()["api"]
    return llm.get_client(
        cfg.get("provider", "claude"),
        os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", ""),
        cfg.get("model", ""),
    )


def ai_analyze_images(screenshots: list[Path], model: str) -> str:
    """AI 只负责一件事：理解图片中的具体内容"""
    client = _client()

    content = [{"type": "text", "text": AI_PROMPT}]
    for ss in screenshots:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encode_image(ss)}"},
        })

    response = client.chat.completions.create(model=model, messages=[{"role": "user", "content": content}])
    return response.choices[0].message.content


# ==================== 代码组装最终报告 ====================

def ai_analyze_text(context: str, model: str, times: str = "") -> str:
    """把本地抽好的文本交给模型理解。

    相对 ai_analyze_images 的意义：模型不再负责「把屏幕上的字读出来」，
    只负责「理解这些字意味着什么」。图像 token 比文本 token 贵一个数量级，
    而读字这件事操作系统本地免费就能做。
    """
    client = _client()
    prompt = AI_TEXT_PROMPT.format(allowed=times or "（以每块开头的时间为准）")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": f"{prompt}\n\n{context}"}],
    )
    return response.choices[0].message.content


def _withheld_note(n: int) -> str:
    """报告里明说扣下了几帧。不说的话就是静默缺失，
    读报告的人会以为那段时间什么都没发生。"""
    if not n:
        return ""
    return (f"\n---\n\n> 另有 {n} 帧来自仅本地应用，未发送给模型，"
            f"因此不在上面的内容里。\n> 它们仍留在本机，"
            f"可用 search_screen / timeline 检索（这两个工具不联网）。\n")


def assemble_report(
    hour: int,
    timeline: list[dict],
    allocation: dict[str, int],
    ai_content: str,
    withheld: int = 0,
) -> str:
    """代码组装报告：时间线和统计由代码生成，内容描述由 AI 生成"""
    return f"""# {hour:02d}:00 - {hour:02d}:59 活动报告

## 活动时间线
{format_timeline(timeline)}

## 时间分配
{format_allocation(allocation)}

## 具体内容
{ai_content}
{_withheld_note(withheld)}"""


# ==================== 主流程 ====================

def mark_skipped(report_dir: Path, date_str: str, hour: int, reason: str) -> None:
    """记下「这个小时确实不该有报告」。

    不留标记的话，补齐逻辑会认为它还欠着，每小时重试一次，
    永远重试下去 —— 锁屏那几个小时就是这种情况。
    """
    out = report_dir / date_str
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{hour:02d}.skipped").write_text(
        f"{datetime.now().isoformat(timespec='seconds')} {reason}\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date YYYY-MM-DD")
    parser.add_argument("--hour", type=int, help="Hour 0-23")
    args = parser.parse_args()

    config = load_config()
    screenshot_dir = SCRIPT_DIR / config["capture"]["output_dir"]
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    provider = config["api"].get("provider", "claude")
    if provider == "zhipu":
        model = config["api"].get("zhipu_model", "")
        text_model = config["api"].get("zhipu_text_model", model)
    else:
        model = config["api"].get("model", "")
        # 文本路径和视觉路径是两个模型：拿视觉模型跑纯文本，贵且效果更差
        text_model = config["api"].get("text_model", model)

    now = datetime.now()
    date_str = args.date or now.strftime("%Y-%m-%d")
    if args.hour is not None:
        target_hour = args.hour
    else:
        prev = now - timedelta(hours=1)
        target_hour = prev.hour
        if not args.date:
            date_str = prev.strftime("%Y-%m-%d")

    screenshots = get_screenshots(screenshot_dir, date_str, target_hour)
    if not screenshots:
        # 退出码 0：这个小时没数据是正常的（睡觉、关机），不是失败。
        # 用 1 会让调用方每次都记一条「失败」，真出问题时反而看不出来
        print(f"{date_str} {target_hour:02d} 时没有截图，跳过")
        mark_skipped(report_dir, date_str, target_hour, "无截图")
        sys.exit(0)

    # 1. 代码：去重 + 封顶
    key_frames = deduplicate_screenshots(screenshots)
    max_frames = config.get("analysis", {}).get("max_frames_per_hour", 30)
    key_frames = cap_frames(key_frames, max_frames)

    # 2. 代码：生成时间线和统计
    timeline = build_timeline(key_frames)
    allocation = build_time_allocation(timeline)

    # 3. AI：只理解图片内容
    print(f"AI analyzing {len(key_frames)} key frames...")
    analysis_mode = config.get("analysis", {}).get("mode", "text")
    if analysis_mode == "text":
        local_only = config.get("privacy", {}).get("local_only_apps", []) or []
        context, sources = build_text_context(key_frames, local_only)
        print(f"Text context: {len(context)} chars, sources={sources}")
        if len(context) >= _MIN_USEFUL_CHARS:
            # 脱敏在 llm.py 的出网收口统一做，这里只负责把占位符换回来。
            # 必须用同一个实例，否则两边的编号对不上，会还原成别人。
            import llm
            r = llm.redactor()
            if r.term_count:
                print(f"脱敏: {r.term_count} 个术语")
            ai_content = r.restore(
                ai_analyze_text(context, text_model, allowed_times(key_frames))
            )
        else:
            # 抽不到文字有两种：屏幕上本来就没东西（锁屏、黑屏），
            # 或者有画面但都是图。只有后者值得花钱走多模态。
            # 多模态发的是原图，所以 local_only 的帧在这条路上更要挡住。
            visible = [
                f for f in key_frames
                if not looks_blank(f)
                and not is_local_only(read_meta(f).get("app", ""), local_only)
            ]
            if not visible:
                print("屏幕无内容（锁屏或黑屏），不生成报告")
                mark_skipped(report_dir, date_str, target_hour, "屏幕无内容")
                sys.exit(0)
            print(f"文字太少，{len(visible)}/{len(key_frames)} 帧有画面，退回多模态")
            ai_content = ai_analyze_images(visible, model)
    else:
        local_only = config.get("privacy", {}).get("local_only_apps", []) or []
        sendable = [
            f for f in key_frames
            if not is_local_only(read_meta(f).get("app", ""), local_only)
        ]
        if not sendable:
            print("本时段全部来自仅本地应用，不生成报告")
            mark_skipped(report_dir, date_str, target_hour, "全部为仅本地应用")
            sys.exit(0)
        ai_content = ai_analyze_images(sendable, model)

    # 4. 代码：组装报告
    report = assemble_report(
        target_hour, timeline, allocation, ai_content, sources.get("local-only", 0)
    )

    output_dir = report_dir / date_str
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{target_hour:02d}.md"
    output_file.write_text(report)
    print(f"Report saved to {output_file}")

    config = load_config()
    if config.get("rag", {}).get("enabled", False):
        from rag import index_report
        try:
            index_report(date_str, target_hour, report)
        except Exception as e:
            print(f"Warning: index failed: {e}")


if __name__ == "__main__":
    main()
