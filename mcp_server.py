#!/usr/bin/env python3
"""拾遗的 MCP server —— 把工作记忆暴露给 AI agent。

为什么要这一层：看板要主动打开才会用，而人不会主动打开。
接成 MCP 之后，「上周那个 40164 报错我怎么解的」可以在 Claude Code 里
顺口问出来 —— 工具长进日常工作流，才会被真正使用。

四个 tool 刻意分开精确与模糊两类，不做成一个万能 search：
  timeline / search_screen  读原始记录，精确、零成本、无幻觉
  recall / read_report      走报告与向量检索，模糊、能理解语义
混成一个会让精确查询也走语义检索，白白降低准确率。
"""

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

# MCP client 启动时不继承 shell 环境，凭证只能从 .env 取
import env_file  # noqa: E402

env_file.load()

mcp = MCPServer("gleaner")


def _config() -> dict:
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def _dirs() -> tuple[Path, Path]:
    cfg = _config()
    return (
        SCRIPT_DIR / cfg["capture"]["output_dir"],
        SCRIPT_DIR / cfg["report"]["output_dir"],
    )


def _hidden_note(n: int) -> str:
    """明说挡了多少 —— 静默过滤比不过滤更糟，用的人会以为那段时间是空的。"""
    if not n:
        return ""
    return (f"\n\n（另有 {n} 条来自仅本地应用，已排除。这些工具的返回值会进入对话、"
            f"随之出网，所以默认不返回。确需查看请传 include_local_only=True，"
            f"或直接在本机看板上看。）")


def _local_only_apps() -> list[str]:
    return _config().get("privacy", {}).get("local_only_apps", []) or []


def _meta_app(meta_file: Path) -> str:
    try:
        lines = meta_file.read_text().strip().splitlines()
    except OSError:
        return ""
    app = lines[0] if lines else ""
    return "" if app.startswith("pid=") else app


def _is_local_only(app: str, apps: list[str]) -> bool:
    low = (app or "").lower()
    return any(k.lower() in low for k in apps)


def _blocked_stems(day_dir: Path, apps: list[str]) -> set[str]:
    """这一天里属于「仅本地应用」的帧。

    为什么工具层也要挡：privacy.local_only_apps 原本只挡住 analyze.py
    那条出网路径，而 MCP 工具的返回值会进入 AI 客户端的对话，
    同样是出网 —— 实测 search_screen 会把钉钉群里的内容原样吐出来。
    四个工具「不联网」说的是它们自己不发请求，不等于结果不会被转发。

    默认挡住，需要时用 include_local_only=True 显式打开 ——
    在本机看板上看这些内容没问题，问题在于经由 AI 客户端转出去。
    """
    if not apps or not day_dir.exists():
        return set()
    return {
        m.stem for m in day_dir.glob("*.meta")
        if _is_local_only(_meta_app(m), apps)
    }


def _date_range(days: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


@mcp.tool()
def timeline(
    days_ago: int = 0, hour_from: int = 0, hour_to: int = 23,
    include_local_only: bool = False,
) -> str:
    """看某一天用了哪些应用、什么窗口，按时间排列。

    这是精确查询，直接读采集时落的元数据，不经过任何模型，没有幻觉。
    days_ago=0 是今天，1 是昨天。适合回答「我周三下午在干什么」。

    config 里 privacy.local_only_apps 列出的应用默认排除 —— 这个工具的
    返回值会进入对话、随之出网。确实需要时把 include_local_only 设成 True。
    """
    screenshot_dir, _ = _dirs()
    day = (date.today() - timedelta(days=days_ago)).isoformat()
    day_dir = screenshot_dir / day
    if not day_dir.exists():
        return f"{day} 没有采集记录"

    apps = [] if include_local_only else _local_only_apps()
    rows, hidden = [], 0
    for meta_file in sorted(day_dir.glob("*.meta")):
        hh = int(meta_file.stem.split("-")[0])
        if not (hour_from <= hh <= hour_to):
            continue
        app = _meta_app(meta_file)
        if _is_local_only(app, apps):
            hidden += 1
            continue
        lines = meta_file.read_text().splitlines()
        title = lines[1] if len(lines) > 1 else ""
        rows.append(f"{meta_file.stem.replace('-', ':')}  {app}  |  {title}")

    note = _hidden_note(hidden)
    if not rows:
        return f"{day} {hour_from}:00-{hour_to}:59 没有记录{note}"
    return f"# {day} 活动时间线（{len(rows)} 条）{note}\n\n" + "\n".join(rows)


@mcp.tool()
def search_screen(
    keyword: str, days: int = 14, max_hits: int = 30,
    include_local_only: bool = False,
) -> str:
    """在屏幕文本里搜关键词，返回命中的时刻和上下文。

    搜的是采集时抓的无障碍文本与 OCR 缓存，属精确匹配。
    适合回答「上次那个报错的原文是什么」「我在哪见过这个词」。

    config 里 privacy.local_only_apps 列出的应用默认排除 —— 命中的原文
    会进入对话、随之出网。确实需要时把 include_local_only 设成 True。
    """
    screenshot_dir, _ = _dirs()
    apps = [] if include_local_only else _local_only_apps()

    # 走 SQLite 索引；索引不在或出问题就退回 grep —— 文件才是根，
    # 索引只是加速层，坏了不该让搜索整个不能用
    try:
        import store
        db = store.connect()
        if store.stats(db)["frames"]:
            days = _date_range(days)
            rows = store.search(db, keyword, days=days, apps_exclude=apps, limit=max_hits)
            hidden = store.count_excluded(db, keyword, days, apps) if apps else 0
            hits = []
            needle = keyword.lower()
            for r in rows:
                stamp = f"{r['day']} {r['ts'].replace('-', ':')}"
                if r["display"] != "active":
                    stamp += f" [副屏{r['display'][1:]}]"
                for line in (r["text"] or "").splitlines():
                    if needle in line.lower():
                        hits.append(f"[{stamp}] {line.strip()[:180]}")
                        break
            db.close()
            return _format_hits(keyword, hits, truncated=len(rows) >= max_hits) + _hidden_note(hidden)
        db.close()
    except Exception as e:
        print(f"索引不可用，退回 grep: {e}", file=sys.stderr)

    needle = keyword.lower()
    hits = []
    hidden = 0

    for day in _date_range(days):
        day_dir = screenshot_dir / day
        if not day_dir.exists():
            continue
        blocked = _blocked_stems(day_dir, apps)
        for text_file in sorted(day_dir.glob("*.txt")) + sorted(day_dir.glob("*.ocr")):
            try:
                content = text_file.read_text()
            except Exception:
                continue
            if needle not in content.lower():
                continue
            if text_file.stem in blocked:
                hidden += 1
                continue
            for line in content.splitlines():
                if needle in line.lower():
                    stamp = f"{day} {text_file.stem.replace('-', ':')}"
                    hits.append(f"[{stamp}] {line.strip()[:180]}")
                    if len(hits) >= max_hits:
                        return _format_hits(keyword, hits, truncated=True) + _hidden_note(hidden)
    return _format_hits(keyword, hits, truncated=False) + _hidden_note(hidden)


def _format_hits(keyword: str, hits: list[str], truncated: bool) -> str:
    if not hits:
        return f"没有找到「{keyword}」。可能是当时没采集到，或该应用不暴露文本。"
    tail = "\n\n（已达上限，可能还有更多）" if truncated else ""
    return f"# 「{keyword}」命中 {len(hits)} 处\n\n" + "\n".join(hits) + tail


@mcp.tool()
def recall(question: str, days: int = 30, top_k: int = 10) -> str:
    """用自然语言问过去发生的事，走语义检索。

    和 search_screen 的分工：那个搜字面，这个搜意思。
    「我最近在纠结什么技术选型」这类问题只有这个答得了。
    """
    try:
        import rag
    except Exception as e:
        return f"RAG 不可用：{e}"

    cfg = _config().get("rag", {})
    if not cfg.get("enabled"):
        return "RAG 未启用（config.yaml 的 rag.enabled 为 false）"

    try:
        results = rag.search(question, top_k=top_k)
    except Exception as e:
        return f"检索失败：{e}"

    if not results:
        return f"没有检索到与「{question}」相关的记录"

    # rag.search 返回的是扁平结构（content/date/hour/section/distance），
    # 不是 chromadb 原始的 document/metadata。取错字段会得到一堆空块。
    blocks = []
    for r in results:
        stamp = f"{r.get('date', '?')} {r.get('hour', '?'):0>2}时"
        section = r.get("section", "")
        text = (r.get("content") or r.get("full_content") or "").strip()
        if not text:
            continue
        head = f"[{stamp}]" + (f" {section}" if section else "")
        blocks.append(f"{head}\n{text[:400]}")

    if not blocks:
        return f"检索到 {len(results)} 条记录但内容为空，可能索引损坏，试试重建：python rag.py"
    return (f"# 关于「{question}」找到 {len(blocks)} 段\n"
            f"（语义检索，可能有偏差；要精确原文用 search_screen）\n\n"
            + "\n\n".join(blocks))


@mcp.tool()
def read_report(days_ago: int = 0, hour: int | None = None) -> str:
    """读已生成的分析报告。hour 不传则读当天所有小时的报告。"""
    _, report_dir = _dirs()
    day = (date.today() - timedelta(days=days_ago)).isoformat()
    day_dir = report_dir / day
    if not day_dir.exists():
        return f"{day} 还没有报告"

    files = sorted(day_dir.glob("*.md"))
    if hour is not None:
        files = [f for f in files if f.stem.startswith(f"{hour:02d}")]
    if not files:
        return f"{day} 没有匹配的报告"

    return "\n\n---\n\n".join(f"## {f.stem}\n\n{f.read_text()}" for f in files)


@mcp.tool()
def capture_status() -> str:
    """采集是否在跑、今天攒了多少数据。排查「怎么没记录」时先看这个。"""
    screenshot_dir, report_dir = _dirs()
    today = date.today().isoformat()
    day_dir = screenshot_dir / today

    shots = (len(list(day_dir.glob("*.png"))) + len(list(day_dir.glob("*.jpg")))) if day_dir.exists() else 0
    ax = len(list(day_dir.glob("*.txt"))) if day_dir.exists() else 0
    ocr_cached = len(list(day_dir.glob("*.ocr"))) if day_dir.exists() else 0
    days = len([d for d in screenshot_dir.iterdir() if d.is_dir()]) if screenshot_dir.exists() else 0
    reports = len(list(report_dir.rglob("*.md"))) if report_dir.exists() else 0

    pid_file = SCRIPT_DIR / "capture.pid"
    running = "未知"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            running = "运行中"
        except Exception:
            running = "已停止"

    return (
        f"采集进程：{running}\n"
        f"今天（{today}）：{shots} 张截图、{ax} 份无障碍文本、{ocr_cached} 份 OCR 缓存\n"
        f"累计：{days} 天数据、{reports} 份报告"
    )


if __name__ == "__main__":
    mcp.run()
