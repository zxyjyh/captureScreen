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
from llm import ZhipuClient as ZhipuAI

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
        if f.is_file() and f.name.startswith(hour_prefix) and f.suffix == ".png"
    )


def deduplicate_screenshots(screenshots: list[Path], threshold: float = _DIFF_THRESHOLD) -> list[Path]:
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
    print(f"Dedup: {len(screenshots)} -> {len(kept)}")
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
        lines = meta_file.read_text().strip().split("\n", 1)
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

    三级来源，按「准确度 × 成本」排：
      1. .txt   采集时抓的无障碍树 —— 结构化、准确、零成本，但只覆盖有可见窗口的应用
      2. .ocr   Apple Vision 本地 OCR —— 全覆盖、零成本，但会把图标误读成碎字
      3. 空     两者都没有，交给调用方决定要不要退回多模态

    OCR 结果落盘缓存：重跑分析时不必再算一遍，一张图 1.3 秒不算便宜。
    """
    ax_file = screenshot.with_suffix(".txt")
    if ax_file.exists():
        text = ax_file.read_text().strip()
        if len(text) >= _MIN_USEFUL_CHARS:
            return text, "accessibility"

    cache = screenshot.with_suffix(".ocr")
    if cache.exists():
        return cache.read_text().strip(), "ocr-cached"

    try:
        text = "\n".join(ocr.recognize(str(screenshot)))
    except Exception as e:
        print(f"OCR failed on {screenshot.name}: {e}")
        return "", "none"

    cache.write_text(text)
    return text, "ocr"


def build_text_context(screenshots: list[Path]) -> tuple[str, dict[str, int]]:
    """把关键帧拼成给模型的文本上下文，并统计各来源占比。"""
    blocks: list[str] = []
    stats: dict[str, int] = {}
    for ss in screenshots:
        text, source = frame_text(ss)
        stats[source] = stats.get(source, 0) + 1
        if not text:
            continue
        meta = read_meta(ss)
        header = f"[{extract_time_from_filename(ss)}] {meta.get('app', '')} | {meta.get('title', '')}"
        blocks.append(f"{header}\n{text}")
    return "\n\n---\n\n".join(blocks), stats


AI_TEXT_PROMPT = """你是一个个人工作记录员。下面是用户这段时间的屏幕文本，
按时间顺序排列，每块以 [时间] 应用 | 窗口标题 开头，正文是该时刻屏幕上的文字。

这些文字来自无障碍树或 OCR，会有噪音（图标误读、菜单项、快捷键）。忽略噪音，
抓实质内容。

按以下结构输出：

## 活动流

按时间还原用户做了什么，每步写明：时间、应用、具体在做什么。
步骤之间如果有因果关系（因为遇到 X 所以去查 Y），写出来。

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


AI_PROMPT = """你是一个个人活动分析师。分析用户这组屏幕截图，还原用户这段时间的完整活动流和心理轨迹。

**重要：文字内容处理规则**
如果截图中包含大量文字（文章、文档、网页正文），你需要：
1. 逐张截图提取文字内容
2. 比较多张截图的文字，判断是否是同一篇文章/文档的不同部分（滚动产生的）
3. 如果是同一篇文章的不同部分，把所有部分拼接成完整内容，然后总结全文核心观点
4. 如果是不同文章/文档，分别总结各自内容
5. 去除重复内容，只保留独特的文字片段

按以下结构输出：

## 活动流

按步骤还原用户的完整操作路径，每一步写清楚具体做了什么：

**第一步**：[时间] [应用] 具体操作内容
**第二步**：[时间] [应用] 具体操作内容
...

要求：
- 每一步必须写明时间（从截图文件名推断）、使用的应用、具体操作
- 步骤之间说明因果关系（比如"因为第一步遇到了XX问题，所以第二步去查了XX文档"）
- 如果用户在同一个应用内做了多件不同的事，拆成多个步骤
- 不要遗漏任何截图中的活动

## 文章内容总结

如果截图中包含文章或长文文档：
- 列出每篇文章/文档的标题和来源
- 拼接同一篇文章在多张截图中的不同部分，形成完整内容
- 用 3-5 个要点总结每篇文章的核心观点
- 标注哪些截图属于同一篇文章（如"截图1-3是同一篇文章的上中下部分"）

如果没有文章内容，写"本时段无长文阅读"。

## 关键内容

提取截图中看到的重要信息：
- 代码文件名、函数名、代码片段
- 对话/消息的要点
- 关键数据、参数、配置信息

## 意图分析

推测用户当时的心态和意图：
- 用户在追求什么目标？
- 当前的进展如何？卡住了还是顺利推进？
- 有没有走偏或者被打断？

## 亮点

这段时间做得好的地方、有价值的发现、学到的新知识。

## 可改进

这段时间可以优化的地方，比如注意力分散、效率低下的时段、不必要的切换。"""


def encode_image(filepath: Path) -> str:
    img = Image.open(filepath).convert("RGB")
    if img.size[0] > _MAX_IMAGE_WIDTH:
        ratio = _MAX_IMAGE_WIDTH / img.size[0]
        img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def ai_analyze_images(screenshots: list[Path], model: str) -> str:
    """AI 只负责一件事：理解图片中的具体内容"""
    api_key = os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
    client = ZhipuAI(api_key=api_key)

    content = [{"type": "text", "text": AI_PROMPT}]
    for ss in screenshots:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encode_image(ss)}"},
        })

    response = client.chat.completions.create(model=model, messages=[{"role": "user", "content": content}])
    return response.choices[0].message.content


# ==================== 代码组装最终报告 ====================

def ai_analyze_text(context: str, model: str) -> str:
    """把本地抽好的文本交给模型理解。

    相对 ai_analyze_images 的意义：模型不再负责「把屏幕上的字读出来」，
    只负责「理解这些字意味着什么」。图像 token 比文本 token 贵一个数量级，
    而读字这件事操作系统本地免费就能做。
    """
    api_key = os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
    client = ZhipuAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": f"{AI_TEXT_PROMPT}\n\n{context}"}],
    )
    return response.choices[0].message.content


def assemble_report(hour: int, timeline: list[dict], allocation: dict[str, int], ai_content: str) -> str:
    """代码组装报告：时间线和统计由代码生成，内容描述由 AI 生成"""
    return f"""# {hour:02d}:00 - {hour:02d}:59 活动报告

## 活动时间线
{format_timeline(timeline)}

## 时间分配
{format_allocation(allocation)}

## 具体内容
{ai_content}
"""


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date YYYY-MM-DD")
    parser.add_argument("--hour", type=int, help="Hour 0-23")
    args = parser.parse_args()

    config = load_config()
    screenshot_dir = SCRIPT_DIR / config["capture"]["output_dir"]
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    model = config["api"]["model"]

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
        print(f"No screenshots for {date_str} {target_hour:02d}")
        sys.exit(1)

    # 1. 代码：去重
    key_frames = deduplicate_screenshots(screenshots)

    # 2. 代码：生成时间线和统计
    timeline = build_timeline(key_frames)
    allocation = build_time_allocation(timeline)

    # 3. AI：只理解图片内容
    print(f"AI analyzing {len(key_frames)} key frames...")
    analysis_mode = config.get("analysis", {}).get("mode", "text")
    if analysis_mode == "text":
        context, sources = build_text_context(key_frames)
        print(f"Text context: {len(context)} chars, sources={sources}")
        if len(context) < _MIN_USEFUL_CHARS:
            print("Text too thin, falling back to vision")
            ai_content = ai_analyze_images(key_frames, model)
        else:
            ai_content = ai_analyze_text(context, model)
    else:
        ai_content = ai_analyze_images(key_frames, model)

    # 4. 代码：组装报告
    report = assemble_report(target_hour, timeline, allocation, ai_content)

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
