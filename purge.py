#!/usr/bin/env python3
"""删除采集到的屏幕数据。

写这个是因为「怎么删」不该需要用户自己去找六个地方 ——
截图、无障碍文本、OCR 缓存、报告、向量索引、日志散在不同目录，
漏掉向量库尤其常见：报告删了，它的向量还在，检索照样能命中原文。
一个隐私工具如果没有干净的删除入口，是不该让人装的。

默认只列出要删什么，加 --yes 才真删。删除不可逆，这个默认不该反过来。

  python purge.py                        看看有什么（不删）
  python purge.py --day 2026-09-01 --yes 删某一天
  python purge.py --before 2026-09-01 --yes  删某天之前的全部
  python purge.py --all --yes            删全部屏幕数据
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent


def _config() -> dict:
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def _dirs() -> tuple[Path, Path, Path]:
    cfg = _config()
    return (
        SCRIPT_DIR / cfg["capture"]["output_dir"],
        SCRIPT_DIR / cfg["report"]["output_dir"],
        SCRIPT_DIR / cfg.get("rag", {}).get("db_path", "chroma_db"),
    )


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def target_dates(shots: Path, reports: Path, args) -> list[str]:
    """按参数选出要清理的日期。"""
    names = set()
    for base in (shots, reports):
        if base.exists():
            names |= {d.name for d in base.iterdir() if d.is_dir()}

    def is_date(n: str) -> bool:
        try:
            datetime.strptime(n, "%Y-%m-%d")
            return True
        except ValueError:
            return False  # reports/weekly 这类目录不参与按日清理

    dates = sorted(n for n in names if is_date(n))
    if args.all:
        return dates
    if args.day:
        return [d for d in dates if d == args.day]
    if args.before:
        return [d for d in dates if d < args.before]
    return dates  # 纯查看


def _forget_index(dates: list[str]) -> None:
    """数据删了索引必须跟着删，否则搜出来的是幽灵条目。"""
    try:
        import store
        db = store.connect()
        for d in dates:
            store.forget_day(db, d)
        db.commit()
        db.close()
    except Exception as e:
        print(f"  ! 索引清理失败（可运行 python store.py 重建）：{e}")


def purge_vectors(db_path: Path, dates: list[str], dry_run: bool) -> int:
    """删掉这些日期的向量。

    报告删了向量还在的话，recall 仍会把原文捞出来 —— 这是最容易漏的一处。
    """
    if not db_path.exists() or not dates:
        return 0
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(db_path))
    except Exception as e:
        print(f"  ! 向量库打不开，跳过：{e}")
        return 0

    total = 0
    for col_name in [c if isinstance(c, str) else c.name for c in client.list_collections()]:
        col = client.get_collection(col_name)
        for date in dates:
            try:
                hit = col.get(where={"date": date})
                ids = hit.get("ids") or []
                if not ids:
                    continue
                total += len(ids)
                if not dry_run:
                    col.delete(ids=ids)
            except Exception as e:
                print(f"  ! {col_name}/{date} 处理失败：{e}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="删除采集到的屏幕数据")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--day", help="只删这一天，格式 2026-09-01")
    scope.add_argument("--before", help="删这一天之前的全部（不含这天）")
    scope.add_argument("--all", action="store_true", help="删全部屏幕数据")
    ap.add_argument("--logs", action="store_true", help="连同日志与看板批注一起删")
    ap.add_argument("--yes", action="store_true", help="真的执行。不加就只是看看")
    args = ap.parse_args()

    shots, reports, db_path = _dirs()
    dates = target_dates(shots, reports, args)
    dry = not args.yes
    selecting = bool(args.all or args.day or args.before)

    if not dates:
        print("没有匹配的日期。")
        return 0

    print(f"目录: {SCRIPT_DIR}\n")
    total = 0
    rows = []
    for date in dates:
        s, r = shots / date, reports / date
        n_files = (len(list(s.iterdir())) if s.exists() else 0) + \
                  (len(list(r.iterdir())) if r.exists() else 0)
        size = _size(s) + _size(r)
        total += size
        rows.append((date, n_files, size))

    print(f"{'日期':12s} {'文件':>6s} {'占用':>9s}")
    for date, n, size in rows:
        print(f"{date:12s} {n:6d} {_human(size):>9s}")
    print(f"{'合计':12s} {sum(r[1] for r in rows):6d} {_human(total):>9s}")

    n_vec = purge_vectors(db_path, dates, dry_run=True)
    print(f"\n关联向量: {n_vec} 条")

    if not selecting:
        print("\n以上是全部数据。要删除请指定范围：")
        print("  python purge.py --day 2026-09-01 --yes")
        print("  python purge.py --before 2026-09-01 --yes")
        print("  python purge.py --all --yes")
        _remind_backups()
        return 0

    if dry:
        print("\n这是预览，什么都没删。确认无误后加 --yes 执行。")
        _remind_backups()
        return 0

    for date in dates:
        for base in (shots, reports):
            d = base / date
            if d.exists():
                shutil.rmtree(d)
                print(f"已删 {d}")
    purge_vectors(db_path, dates, dry_run=False)
    print(f"已删向量 {n_vec} 条")

    if args.logs:
        for name in ("capture.log", "annotations.json"):
            f = SCRIPT_DIR / name
            if f.exists():
                f.unlink()
                print(f"已删 {f}")

    import audit
    audit.record("命令行 purge.py",
                 ("--all" if args.all else f"--day {args.day}" if args.day else f"--before {args.before}")
                 + "：" + "、".join(dates), len(dates), total)
    print(f"\n完成，释放 {_human(total)}。")
    _remind_backups()
    return 0


def _remind_backups() -> None:
    """备份目录不自动删 —— 它是 git 仓库，删了就真没了，该由人决定。"""
    baks = sorted(SCRIPT_DIR.glob(".git.bak-*"))
    if not baks:
        return
    print("\n另外注意，这些备份里也可能有旧截图，本工具不会碰：")
    for b in baks:
        print(f"  {b.name}  {_human(_size(b))}    删除: rm -rf {b}")


if __name__ == "__main__":
    sys.exit(main())
