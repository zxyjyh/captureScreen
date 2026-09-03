#!/usr/bin/env python3
"""日总结：每天自动跑一次。

为什么不是「先补齐全天的小时报告再汇总」：那样成本和每小时跑一次完全一样，
一天十几次模型调用，什么都没省下来。日总结直接从屏幕文本出发，
一天一次调用。

已经存在的小时报告会被复用 —— 那是用户主动点过、已经付过钱的结果，
比原始文本更精炼。没有报告的时段才回落到原始文本。
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent

# launchd / cron 启动时不继承 shell 环境，凭证只能从 .env 取
import env_file  # noqa: E402

env_file.load()

sys.path.insert(0, str(SCRIPT_DIR))
import analyze  # noqa: E402
import llm  # noqa: E402
import tasks as task_cfg  # noqa: E402

# 一天最多送多少帧的原始文本。小时级上限是 30，一天按 24 小时算不能简单
# 相乘 —— 那会让一次日总结比全天分开跑还贵。
_MAX_DAY_FRAMES = 60


def load_config() -> dict:
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def existing_hour_reports(report_dir: Path, date_str: str) -> dict[int, str]:
    """已有的小时报告，取其中的实质小节。

    只抽「活动流」和「关键事实」：时间线和时间分配是代码算出来的统计，
    日总结自己也会算一遍，送进去只是重复占 token。
    """
    day_dir = report_dir / date_str
    if not day_dir.exists():
        return {}

    out: dict[int, str] = {}
    for rf in sorted(day_dir.glob("[0-9][0-9].md")):
        text = rf.read_text()
        parts = []
        for name in ("活动流", "关键事实", "阅读与信息摄入"):
            m = re.search(rf"## {name}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
            if m and m.group(1).strip():
                parts.append(f"## {name}\n{m.group(1).strip()}")
        if parts:
            out[int(rf.stem)] = "\n\n".join(parts)
    return out


def build_day_context(
    screenshot_dir: Path, report_dir: Path, date_str: str, local_only: list[str]
) -> tuple[str, dict]:
    """拼出全天的上下文，返回 (文本, 统计)。"""
    reports = existing_hour_reports(report_dir, date_str)
    blocks: list[str] = []
    stats = {"hours_from_report": 0, "hours_from_text": 0, "frames": 0}

    # 没有报告的时段：收集原始帧，全天统一封顶
    raw_hours: dict[int, list[Path]] = {}
    for hour in range(24):
        if hour in reports:
            continue
        shots = analyze.get_screenshots(screenshot_dir, date_str, hour)
        if shots:
            raw_hours[hour] = analyze.deduplicate_screenshots(shots)

    total_raw = sum(len(v) for v in raw_hours.values())
    if total_raw > _MAX_DAY_FRAMES and total_raw:
        # 按各小时的帧数等比例分配名额，活跃的小时分得多
        for hour, shots in raw_hours.items():
            quota = max(1, round(len(shots) / total_raw * _MAX_DAY_FRAMES))
            raw_hours[hour] = analyze.cap_frames(shots, quota)

    for hour in range(24):
        if hour in reports:
            blocks.append(f"### {hour:02d}:00\n{reports[hour]}")
            stats["hours_from_report"] += 1
        elif hour in raw_hours:
            text, _ = analyze.build_text_context(raw_hours[hour], local_only)
            if text.strip():
                blocks.append(f"### {hour:02d}:00（原始屏幕文本）\n{text}")
                stats["hours_from_text"] += 1
                stats["frames"] += len(raw_hours[hour])

    return "\n\n---\n\n".join(blocks), stats


DAILY_PROMPT = """你是一个个人工作记录员。下面是用户一整天的屏幕记录，
按小时排列。有的小时是已经整理过的报告，有的是原始屏幕文本（会有 OCR 噪音）。

把这一天压缩成一份可检索的记录，按以下结构输出：

## 这一天做了什么

按时间顺序，用 3-8 条写清楚推进了哪几件事。一件事跨多个小时的，
合成一条，写明起止时间。不要逐小时复述。

## 待办

明确提到、但今天没做完的事。每条一行，写清楚**要做什么**，
能写出触发条件（等谁、等什么）就一并写上。没有就写「无」。

## 悬而未决

今天开了头、也没有结论的事 —— 和待办的区别是：待办知道下一步做什么，
悬而未决是还不知道该怎么办。没有就写「无」。

## 关键事实

汇总全天的具体信息。**每组**最多 6 条，超过就合并同类或丢掉次要的**，不要因为都重要就全留；
每条**一句话说完**，展开理由放到那件事本身的记录里**，捡最重要的写 ——
这是一天的总结，不是文件清单；要查全的细节到小时报告里看。
有就写，没有就跳过该组：

- **项目与主题**：这一天涉及的项目、仓库、产品
- **改动重点**：主要改了哪几处，写清楚改的是什么、为什么改。
  不要罗列每一个碰过的文件
- **报错与问题**：具体的错误码、错误信息、卡住的地方
- **决定与结论**：做了什么判断、选了哪个方案
- **链接**：真正需要回访的地址，不是全天点开过的所有页面

要求：
- **不要嵌套列表**。每条就是一条，需要展开就分成几条平级的
- 只写记录里真实出现过的内容，**不要推测、不要补全、不要发挥**
- 不做效率点评、不给改进建议、不写明日计划、不统计时长 ——
  这些没有检索价值，且极易变成虚构（时长由代码统计，不用你写）
- 具体优先于概括：写「修 40164 IP 白名单报错」，不写「处理了技术问题」
"""


def ai_daily(context: str, model: str, provider: str, api_key: str) -> str:
    client = llm.get_client(provider, api_key, model)
    r = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": f"{DAILY_PROMPT}\n\n{context}"}]
    )
    return r.choices[0].message.content


def _strip_title(text: str) -> str:
    """剥掉模型自己加的一级标题。

    prompt 要求从「## 这一天做了什么」开始，但模型常常先补一行
    「# 2026-09-02 工作记录」。报告框架已经有标题了，留着就是两个 h1，
    小节标题一放大这个重复更刺眼。
    """
    lines = text.lstrip().splitlines()
    while lines and (not lines[0].strip() or lines[0].startswith("# ")):
        lines.pop(0)
    return "\n".join(lines).strip()


def day_allocation(screenshot_dir: Path, date_str: str) -> dict[str, int]:
    """全天的应用时长，由代码统计 —— 不交给模型，它算不准也没必要算。"""
    total: dict[str, int] = {}
    for hour in range(24):
        shots = analyze.get_screenshots(screenshot_dir, date_str, hour)
        if not shots:
            continue
        timeline = analyze.build_timeline(shots)
        for app, minutes in analyze.build_time_allocation(timeline).items():
            total[app] = total.get(app, 0) + minutes
    return total


def _day_tasks(date_str: str) -> tuple[dict, dict]:
    try:
        import store
        db = store.connect()
        rows = [dict(r) for r in db.execute(
            "SELECT ts, app, title, text FROM frames WHERE day=? ORDER BY ts", (date_str,))]
        db.close()
        return task_cfg.allocate(rows) if rows else ({}, {})
    except Exception as e:
        print(f"任务统计跳过: {e}")
        return {}, {}


def _fmt_alloc(title: str, data: dict[str, int]) -> str:
    if not data:
        return ""
    total = sum(data.values()) or 1
    lines = [f"- {k}: {v}分钟 ({v * 100 // total}%)"
             for k, v in sorted(data.items(), key=lambda x: -x[1])]
    return f"\n## {title}\n" + "\n".join(lines) + "\n"


def summarize_day(date_str: str) -> Path | None:
    config = load_config()
    screenshot_dir = SCRIPT_DIR / config["capture"]["output_dir"]
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    api = config["api"]
    provider = api.get("provider", "claude")
    model = (api.get("zhipu_text_model") if provider == "zhipu" else api.get("text_model")) or ""
    local_only = config.get("privacy", {}).get("local_only_apps", []) or []

    context, stats = build_day_context(screenshot_dir, report_dir, date_str, local_only)
    if not context.strip():
        print(f"{date_str} 没有可总结的内容，跳过")
        return None

    print(f"日总结 {date_str}：{stats['hours_from_report']} 小时用已有报告，"
          f"{stats['hours_from_text']} 小时用原始文本（{stats['frames']} 帧）")

    import os
    key = os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
    r = llm.redactor()
    ai_content = _strip_title(r.restore(ai_daily(context, model, provider, key)))

    alloc = day_allocation(screenshot_dir, date_str)
    total_min = sum(alloc.values())
    lines = [f"- {app}: {m}分钟 ({m * 100 // max(total_min, 1)}%)"
             for app, m in sorted(alloc.items(), key=lambda x: -x[1])]

    by_task, by_kind = _day_tasks(date_str)
    report = f"""# {date_str} 日总结

## 时间分配
共 {total_min} 分钟
{chr(10).join(lines) if lines else "- 无记录"}
{_fmt_alloc("任务分配", by_task)}{_fmt_alloc("工作性质", by_kind)}
## 具体内容
{ai_content}
"""
    out_dir = report_dir / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "daily-summary.md"
    out_file.write_text(report)
    print(f"已保存 {out_file}")

    if config.get("rag", {}).get("enabled", False):
        try:
            from rag import index_report
            index_report(date_str, 99, report)  # 99 = 日总结，不与小时冲突
        except Exception as e:
            print(f"索引失败: {e}")
    return out_file


def main() -> int:
    parser = argparse.ArgumentParser(description="生成日总结")
    parser.add_argument("--date", help="YYYY-MM-DD，默认今天")
    args = parser.parse_args()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    return 0 if summarize_day(date_str) else 0


if __name__ == "__main__":
    sys.exit(main())
