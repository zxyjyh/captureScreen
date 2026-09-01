"""周期总结脚本 - 生成周报/月报，供长期查询使用"""

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from llm import ZhipuClient as ZhipuAI

SCRIPT_DIR = Path(__file__).resolve().parent

# launchd / cron 启动时不继承 shell 环境，凭证只能从 .env 取
import env_file  # noqa: E402

env_file.load()

WEEKLY_PROMPT = """以下是用户 {start_date} 到 {end_date}（一周）的每日活动总结报告。
请综合生成一份周总结，按以下格式输出：

# {start_date} ~ {end_date} 周总结

## 本周概览
2-3 句话总结本周工作状态

## 核心成果
最重要的 3-5 项成果

## 时间分配
各类活动的大致时间占比

## 关键发现
做得好的 / 需要改进的 / 注意力模式

## 产品灵感
本周所有活动中最值得深入的产品想法（合并重复的）

## 下周计划建议
基于本周进度，下周应优先做什么"""

MONTHLY_PROMPT = """以下是用户 {start_date} 到 {end_date}（一个月）的周总结报告。
请综合生成一份月总结，按以下格式输出：

# {start_date} ~ {end_date} 月总结

## 本月概览
3-4 句话总结本月工作

## 核心成果
最重要的 5-8 项成果

## 时间分配趋势
各周时间分配变化趋势

## 关键发现与模式
月度维度的工作模式、习惯变化

## 产品灵感精选
本月最有潜力的 2-3 个产品想法

## 下月建议
下个月的优先级和工作重心"""


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def _get_client():
    api_key = os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
    return ZhipuAI(api_key=api_key)


def _date_range(start: str, end: str) -> list[str]:
    """生成日期范围列表"""
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    dates = []
    current = start_dt
    while current <= end_dt:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def generate_weekly(start_date: str, end_date: str):
    """生成周总结"""
    config = load_config()
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    model = config["api"]["model"]

    dates = _date_range(start_date, end_date)
    summaries = []
    for date_str in dates:
        summary_file = report_dir / date_str / "daily-summary.md"
        if summary_file.exists():
            summaries.append(f"--- {date_str} ---\n{summary_file.read_text()}")

    if not summaries:
        print(f"No daily summaries found for {start_date} ~ {end_date}")
        return

    combined = "\n\n".join(summaries)
    prompt = WEEKLY_PROMPT.format(start_date=start_date, end_date=end_date)

    print(f"Generating weekly summary for {start_date} ~ {end_date} ({len(summaries)} days)...")

    client = _get_client()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": f"{prompt}\n\n{combined}"}],
    )

    year, week, _ = datetime.strptime(start_date, "%Y-%m-%d").isocalendar()
    output_dir = report_dir / "weekly"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{year}-W{week:02d}.md"
    output_file.write_text(response.choices[0].message.content)
    print(f"Weekly summary saved to {output_file}")


def generate_monthly(year: int, month: int):
    """生成月总结"""
    config = load_config()
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    model = config["api"]["model"]

    weekly_dir = report_dir / "weekly"
    month_str = f"{year}-{month:02d}"
    weekly_summaries = []

    # 计算该月的日期范围，用于匹配周报
    month_start = datetime(year, month, 1)
    month_end = datetime(year, month, 28) + timedelta(days=3)  # 28+3=31, clamped by month
    # 精确计算月末
    if month == 12:
        month_end = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = datetime(year, month + 1, 1) - timedelta(days=1)

    if weekly_dir.exists():
        for wf in sorted(weekly_dir.glob("*.md")):
            # 解析周报文件名中的年份和周数
            try:
                parts = wf.stem.split("-W")
                file_year = int(parts[0])
                file_week = int(parts[1])
                # 获取该周周一的日期
                week_monday = datetime.strptime(f"{file_year}-{file_week}-1", "%Y-%%W-%w")
                # 判断该周是否与本月有交集
                week_sunday = week_monday + timedelta(days=6)
                if week_monday <= month_end and week_sunday >= month_start:
                    weekly_summaries.append(f"--- {wf.stem} ---\n{wf.read_text()}")
            except (ValueError, IndexError):
                continue

    if not weekly_summaries:
        print(f"No weekly summaries found for {year}-{month:02d}")
        return

    combined = "\n\n".join(weekly_summaries)
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-28"
    prompt = MONTHLY_PROMPT.format(start_date=start_date, end_date=end_date)

    print(f"Generating monthly summary for {year}-{month:02d} ({len(weekly_summaries)} weeks)...")

    client = _get_client()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": f"{prompt}\n\n{combined}"}],
    )

    output_dir = report_dir / "monthly"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{year}-{month:02d}.md"
    output_file.write_text(response.choices[0].message.content)
    print(f"Monthly summary saved to {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Generate periodic summaries")
    parser.add_argument("--weekly", nargs=2, metavar=("START", "END"), help="Generate weekly summary (YYYY-MM-DD YYYY-MM-DD)")
    parser.add_argument("--monthly", nargs=2, metavar=("YEAR", "MONTH"), help="Generate monthly summary")
    args = parser.parse_args()

    if args.weekly:
        generate_weekly(args.weekly[0], args.weekly[1])
    elif args.monthly:
        generate_monthly(int(args.monthly[0]), int(args.monthly[1]))
    else:
        print("Usage: python period_summary.py --weekly 2026-05-19 2026-05-25")
        print("       python period_summary.py --monthly 2026 5")


if __name__ == "__main__":
    main()
