"""自然语言查询工具 - 金字塔式检索：近用详细报告，远用周/月总结"""

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

QUERY_SYSTEM_PROMPT = "根据检索到的历史活动记录回答用户问题，中文回复。信息不足时如实说明。"

QUERY_TEMPLATE = "问题：{query}\n\n相关记录：\n{context}"

# 金字塔层级阈值
_NEAR_DAYS = 3      # 近3天：用小时报告（最详细）
_MID_DAYS = 30      # 3-30天：用日总结（中等粒度）
# 30天以上：用周/月总结（最粗略）


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def _collect_period_summaries(report_dir: Path, date_from: str, date_to: str) -> list[str]:
    """从周报/月报中收集范围内的摘要文本"""
    texts = []

    # 月报
    monthly_dir = report_dir / "monthly"
    if monthly_dir.exists():
        for mf in sorted(monthly_dir.glob("*.md")):
            texts.append(f"[月报 {mf.stem}]\n{mf.read_text()}")

    # 周报
    weekly_dir = report_dir / "weekly"
    if weekly_dir.exists():
        for wf in sorted(weekly_dir.glob("*.md")):
            texts.append(f"[周报 {wf.stem}]\n{wf.read_text()}")

    return texts


def _collect_daily_summaries(report_dir: Path, date_from: str, date_to: str) -> list[str]:
    """从日总结中收集范围内的摘要文本"""
    texts = []
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        summary_file = report_dir / date_str / "daily-summary.md"
        if summary_file.exists():
            texts.append(f"[{date_str} 日总结]\n{summary_file.read_text()}")
        current += timedelta(days=1)
    return texts


def _collect_hourly_reports(report_dir: Path, date_from: str, date_to: str) -> list[str]:
    """直接读取日期范围内的小时报告文件"""
    texts = []
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        day_dir = report_dir / date_str
        if day_dir.exists():
            for rf in sorted(day_dir.glob("[0-9][0-9].md")):
                texts.append(f"[{date_str} {rf.stem}:00]\n{rf.read_text()}")
        current += timedelta(days=1)
    return texts


def build_context(query: str, report_dir: Path, date_from: str | None, date_to: str | None, top_k: int, rag_enabled: bool = False) -> str:
    """根据时间范围选择合适粒度的数据源，构建上下文"""
    today = datetime.now().strftime("%Y-%m-%d")
    effective_to = date_to or today

    context_parts = []

    if rag_enabled:
        if not date_from:
            from rag import search
            results = search(query=query, date_to=effective_to, top_k=top_k)
            return _format_rag_results(results)

        from_dt = datetime.strptime(date_from, "%Y-%m-%d")
        to_dt = datetime.strptime(effective_to, "%Y-%m-%d")
        span_days = (to_dt - from_dt).days

        if span_days <= _NEAR_DAYS:
            from rag import search
            results = search(query=query, date_from=date_from, date_to=effective_to, top_k=top_k)
            return _format_rag_results(results)

        if span_days <= _MID_DAYS:
            near_start = max(date_from, (to_dt - timedelta(days=_NEAR_DAYS)).strftime("%Y-%m-%d"))
            from rag import search
            results = search(query=query, date_from=near_start, date_to=effective_to, top_k=top_k)
            if results:
                context_parts.append("=== 近期详细记录 ===")
                context_parts.append(_format_rag_results(results))
            daily_texts = _collect_daily_summaries(report_dir, date_from, near_start)
            if daily_texts:
                context_parts.append("=== 早期日总结 ===")
                context_parts.append("\n\n".join(daily_texts))
        else:
            near_start = max(date_from, (to_dt - timedelta(days=_NEAR_DAYS)).strftime("%Y-%m-%d"))
            mid_start = max(date_from, (to_dt - timedelta(days=_MID_DAYS)).strftime("%Y-%m-%d"))
            from rag import search
            results = search(query=query, date_from=near_start, date_to=effective_to, top_k=top_k)
            if results:
                context_parts.append("=== 近期详细记录 ===")
                context_parts.append(_format_rag_results(results))
            daily_texts = _collect_daily_summaries(report_dir, mid_start, near_start)
            if daily_texts:
                context_parts.append("=== 中期日总结 ===")
                context_parts.append("\n\n".join(daily_texts))
            period_texts = _collect_period_summaries(report_dir, date_from, mid_start)
            if period_texts:
                context_parts.append("=== 远期周/月报 ===")
                context_parts.append("\n\n".join(period_texts))
    else:
        # RAG 关闭：直接读取报告文件
        effective_from = date_from or effective_to
        hourly_texts = _collect_hourly_reports(report_dir, effective_from, effective_to)
        if hourly_texts:
            context_parts.append("=== 小时报告 ===")
            context_parts.append("\n\n---\n\n".join(hourly_texts))

        daily_texts = _collect_daily_summaries(report_dir, effective_from, effective_to)
        if daily_texts:
            context_parts.append("=== 日总结 ===")
            context_parts.append("\n\n".join(daily_texts))

    return "\n\n".join(context_parts) if context_parts else ""


def _format_rag_results(results: list[dict]) -> str:
    if not results:
        return ""
    parts = []
    for r in results:
        parts.append(f"[{r['date']} {r['hour']}:00 | {r['section']}] (相关度: {1 - r['distance']:.2f})\n{r['content']}")
    return "\n\n---\n\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Query screen activity history")
    parser.add_argument("query", help="Natural language query")
    parser.add_argument("--from", dest="date_from", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", help="End date (YYYY-MM-DD)")
    parser.add_argument("--top-k", type=int, default=None, help="Number of results to retrieve")
    args = parser.parse_args()

    config = load_config()
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]
    model = config["api"]["model"]
    top_k = args.top_k or config["rag"]["top_k"]
    rag_enabled = config.get("rag", {}).get("enabled", False)

    context = build_context(args.query, report_dir, args.date_from, args.date_to, top_k, rag_enabled)

    if not context:
        print("No relevant records found.")
        return

    print(f"Context built. Generating answer...\n")

    prompt = QUERY_TEMPLATE.format(query=args.query, context=context)

    cfg = load_config()["api"]
    client = llm.get_client(
        cfg.get("provider", "claude"),
        os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", ""),
        cfg.get("model", ""),
    )

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"system": QUERY_SYSTEM_PROMPT},
    )

    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
