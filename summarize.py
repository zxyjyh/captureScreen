"""日总结 - 代码统计 + AI 评语"""

import argparse
import os
import re
from datetime import datetime
from pathlib import Path

import yaml
from zhipuai import ZhipuAI

SCRIPT_DIR = Path(__file__).resolve().parent


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def parse_hourly_reports(report_dir: Path, date_str: str) -> list[dict]:
    """代码：提取所有小时报告的结构化数据"""
    day_dir = report_dir / date_str
    if not day_dir.exists():
        return []

    reports = []
    for rf in sorted(day_dir.glob("[0-9][0-9].md")):
        content = rf.read_text()
        hour = int(rf.stem)

        # 提取时间分配
        allocation = {}
        alloc_match = re.search(r"## 时间分配\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
        if alloc_match:
            for line in alloc_match.group(1).strip().split("\n"):
                m = re.match(r"- (.+?): (\d+)分钟", line)
                if m:
                    allocation[m.group(1)] = int(m.group(2))

        # 提取具体内容
        content_match = re.search(r"## 具体内容\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
        ai_content = content_match.group(1).strip() if content_match else ""

        # 提取时间线
        timeline_match = re.search(r"## 活动时间线\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
        timeline = timeline_match.group(1).strip() if timeline_match else ""

        reports.append({
            "hour": hour,
            "allocation": allocation,
            "content": ai_content,
            "timeline": timeline,
        })

    return reports


def aggregate_stats(reports: list[dict]) -> dict:
    """代码：汇总全天统计"""
    total_allocation = {}
    all_content = []

    for r in reports:
        for app, minutes in r["allocation"].items():
            total_allocation[app] = total_allocation.get(app, 0) + minutes
        if r["content"]:
            all_content.append(f"[{r['hour']:02d}:00] {r['content']}")

    sorted_allocation = dict(sorted(total_allocation.items(), key=lambda x: -x[1]))
    total_minutes = sum(sorted_allocation.values())

    return {
        "total_hours": len(reports),
        "allocation": sorted_allocation,
        "total_minutes": total_minutes,
        "content_parts": all_content,
    }


def format_stats(stats: dict) -> str:
    """代码：格式化统计"""
    lines = []
    for app, minutes in stats["allocation"].items():
        pct = minutes / stats["total_minutes"] * 100 if stats["total_minutes"] else 0
        lines.append(f"- {app}: {minutes}分钟 ({pct:.0f}%)")
    return "\n".join(lines)


def ai_daily_comment(content_summary: str, model: str) -> str:
    """AI：只写评语和建议（输入是压缩后的内容摘要）"""
    prompt = f"""基于以下一天的工作内容摘要，用中文写：
1. 今日亮点（1-2句）
2. 可改进处（1-2句）
3. 明日建议（1-2句）

内容摘要：
{content_summary}"""

    api_key = os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
    client = ZhipuAI(api_key=api_key)
    response = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}])
    return response.choices[0].message.content


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD")
    args = parser.parse_args()

    config = load_config()
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    model = config["api"]["model"]

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    reports = parse_hourly_reports(report_dir, date_str)
    if not reports:
        print(f"No hourly reports for {date_str}")
        return

    # 1. 代码：统计
    stats = aggregate_stats(reports)

    # 2. 代码：组装报告框架（不需要 AI）
    content_summary = "\n".join(stats["content_parts"][:10])  # 只取前10条，控制 token

    print(f"Generating daily summary for {date_str} ({len(reports)} hours)...")

    # 3. AI：只写评语
    ai_comment = ai_daily_comment(content_summary, model)

    # 4. 代码：组装最终报告
    report = f"""# {date_str} 日总结

## 今日概览
活跃 {stats['total_hours']} 小时，共 {stats['total_minutes']} 分钟

## 时间分配
{format_stats(stats)}

## 各小时内容
{"".join(stats['content_parts'])}

## AI 评语
{ai_comment}
"""

    output_file = report_dir / date_str / "daily-summary.md"
    output_file.write_text(report)
    print(f"Saved to {output_file}")


if __name__ == "__main__":
    main()
