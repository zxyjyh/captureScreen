"""周期总结脚本 - 生成周报/月报，供长期查询使用"""

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

import yaml
import llm

SCRIPT_DIR = Path(__file__).resolve().parent

# launchd / cron 启动时不继承 shell 环境，凭证只能从 .env 取
import env_file  # noqa: E402

env_file.load()

WEEKLY_PROMPT = """下面是 {covered} 的每日总结（共 {n} 天）。
把它们合成一份跨天的记录。

**直接从「## 主线」开始写，不要写标题，不要说明数据情况。**
覆盖了哪几天由外面的代码写清楚，你只管从给到的内容里提取。

## 主线

这段时间真正在推进的是哪几件事。每条写：这件事进展到哪一步，
关键的转折是什么。按投入排序，最多 5 条。
跨天的同一件事合成一条，不要按天复述。

## 未完成

跨天仍未做完的事。每条一行，写清楚卡在哪。
只写到这段时间结束时仍然悬着的 —— 当天开当天关的不算。

## 关键事实

跨天反复出现、值得记住的具体信息：项目与仓库、重要的决定、
踩过的坑与结论。每组**最多 6 条，超过就合并同类或丢掉次要的**，不要因为都重要就全留；
每条**一句话说完**，展开理由放到那件事本身的记录里。

要求：
- 只写给到的内容里真实出现过的，**不要推测、不要补全、不要发挥**
- 不做效率点评、不给改进建议、不写下周计划、不统计时长
- 不评价数据是否完整、不跟读者对话
- 具体优先于概括：写「订座系统 H5 会所选择页接上真实接口」，
  不写「推进了前端开发」"""


MONTHLY_PROMPT = """下面是 {covered} 的周总结（共 {n} 份）。
把它们合成一份跨周的记录。

**直接从「## 主线」开始写，不要写标题，不要说明数据情况。**

## 主线

这个月真正在推进的是哪几件事，每条写清楚进展到哪一步。
最多 5 条，按投入排序。

## 未完成

到月末仍然悬着的事。

## 关键事实

跨周反复出现、值得记住的具体信息。每组最多 6 条。

要求：
- 只写给到的内容里真实出现过的，**不要推测、不要补全、不要发挥**
- 不做效率点评、不给改进建议、不写下月计划、不统计时长
- 不评价数据是否完整、不跟读者对话"""


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def _get_client():
    cfg = load_config()["api"]
    return llm.get_client(
        cfg.get("provider", "claude"),
        os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", ""),
        cfg.get("model", ""),
    )


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


def _span_allocation(days: list[str]) -> tuple[dict, dict, dict]:
    """跨天聚合三个维度的时长。

    由代码算而不是问模型：模型手里只有文字总结，算不出分钟数，
    让它猜只会得到一组看着像真的假数字。
    """
    import collections
    by_app: collections.Counter = collections.Counter()
    by_task: collections.Counter = collections.Counter()
    by_kind: collections.Counter = collections.Counter()
    try:
        import store
        import tasks as task_cfg
        import analyze
        cfg = task_cfg.load()
        db = store.connect()
        for day in days:
            rows = [dict(r) for r in db.execute(
                "SELECT ts, app, title, text FROM frames WHERE day=? ORDER BY ts", (day,))]
            if not rows:
                continue
            t, k = task_cfg.allocate(rows, cfg)
            by_task.update(t)
            by_kind.update(k)
            shots = analyze.get_screenshots(
                SCRIPT_DIR / load_config()["capture"]["output_dir"], day, -1)
            for hour in range(24):
                hs = analyze.get_screenshots(
                    SCRIPT_DIR / load_config()["capture"]["output_dir"], day, hour)
                if hs:
                    by_app.update(analyze.build_time_allocation(analyze.build_timeline(hs)))
        db.close()
    except Exception as e:
        print(f"跨天时间统计跳过: {e}")
    return dict(by_task), dict(by_kind), dict(by_app)


def _fmt_alloc(title: str, data: dict) -> str:
    if not data:
        return ""
    total = sum(data.values()) or 1
    lines = [f"- {k}: {v}分钟 ({v * 100 // total}%)"
             for k, v in sorted(data.items(), key=lambda x: -x[1])]
    return f"\n## {title}\n" + "\n".join(lines) + "\n"


def _covered(days: list[str]) -> str:
    """实际有数据的日期，连续就写区间，不连续就逐个列。"""
    if not days:
        return "（无数据）"
    if len(days) == 1:
        return days[0]
    expect = _date_range(days[0], days[-1])
    return f"{days[0]} ~ {days[-1]}" if expect == days else "、".join(days)


def _strip_title(text: str) -> str:
    """剥掉模型自己加的一级标题 —— 报告框架已经有标题。"""
    lines = text.lstrip().splitlines()
    while lines and (not lines[0].strip() or lines[0].startswith("# ")):
        lines.pop(0)
    return "\n".join(lines).strip()


def generate_weekly(start_date: str, end_date: str):
    """生成周总结"""
    config = load_config()
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    api = config["api"]
    provider = api.get("provider", "claude")
    model = (api.get("zhipu_text_model") if provider == "zhipu"
             else api.get("text_model")) or ""

    dates = _date_range(start_date, end_date)
    summaries, found = [], []
    for date_str in dates:
        summary_file = report_dir / date_str / "daily-summary.md"
        if summary_file.exists():
            summaries.append(f"--- {date_str} ---\n{summary_file.read_text()}")
            found.append(date_str)

    if not summaries:
        print(f"No daily summaries found for {start_date} ~ {end_date}")
        return

    combined = "\n\n".join(summaries)
    # 告诉模型实际覆盖了哪几天，而不是「说好的一周」——
    # 说是一周却只给一天，模型会转头跟读者解释数据缺口，那不是报告
    prompt = WEEKLY_PROMPT.format(covered=_covered(found), n=len(found))

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
    body = _strip_title(response.choices[0].message.content)
    # 覆盖范围写在标题下，是事实陈述，不是让模型去评论的东西
    note = "" if len(found) == len(dates) else f"（{len(dates)} 天中有 {len(found)} 天记录）"
    by_task, by_kind, by_app = _span_allocation(found)
    alloc = (_fmt_alloc("任务分配", by_task) + _fmt_alloc("工作性质", by_kind)
             + _fmt_alloc("时间分配", by_app))
    output_file.write_text(
        f"# {_covered(found)} 周总结{note}\n{alloc}\n{body}\n"
    )
    print(f"Weekly summary saved to {output_file}")


def generate_monthly(year: int, month: int):
    """生成月总结"""
    config = load_config()
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    api = config["api"]
    provider = api.get("provider", "claude")
    model = (api.get("zhipu_text_model") if provider == "zhipu"
             else api.get("text_model")) or ""

    weekly_dir = report_dir / "weekly"
    month_str = f"{year}-{month:02d}"
    weekly_summaries, found = [], []

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
                    found.append(wf.stem)
            except (ValueError, IndexError):
                continue

    if not weekly_summaries:
        print(f"No weekly summaries found for {year}-{month:02d}")
        return

    combined = "\n\n".join(weekly_summaries)
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-28"
    # 月报收的是周报，found 里是 2026-W36 这种周编号，不是日期
    prompt = MONTHLY_PROMPT.format(covered="、".join(found) or "（无）", n=len(found))

    print(f"Generating monthly summary for {year}-{month:02d} ({len(weekly_summaries)} weeks)...")

    client = _get_client()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": f"{prompt}\n\n{combined}"}],
    )

    output_dir = report_dir / "monthly"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{year}-{month:02d}.md"
    body = _strip_title(response.choices[0].message.content)
    output_file.write_text(f"# {year}-{month:02d} 月总结\n\n{body}\n")
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
