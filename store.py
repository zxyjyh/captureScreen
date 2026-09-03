"""屏幕文本的 SQLite 索引。

**文件是唯一的事实来源，这个库只是索引。** 任何时候都能从
screenshots/ 重建；删了也只是搜索变慢，不会丢数据。
一个以「数据是你自己的纯文件」为前提的工具，不该把内容锁进数据库。

中文检索的坑：FTS5 默认的 unicode61 分词器不切中文 —— 整句话变成
一个 token，搜「白名单」一条都命中不了。trigram 分词器按三字符切，
中文全对，但代价是 2 字符的查询（IP、DB）搜不到。
所以两个索引都建：中文和长词走 trigram，短的 ASCII 词走 unicode61。
"""

import pathlib
import sqlite3
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "index.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS frames (
    id      INTEGER PRIMARY KEY,
    day     TEXT NOT NULL,
    ts      TEXT NOT NULL,
    display TEXT NOT NULL DEFAULT 'active',
    app     TEXT,
    title   TEXT,
    source  TEXT,
    chars   INTEGER DEFAULT 0,
    text    TEXT NOT NULL DEFAULT '',
    UNIQUE(day, ts, display)
);
CREATE INDEX IF NOT EXISTS idx_frames_day ON frames(day);
CREATE INDEX IF NOT EXISTS idx_frames_app ON frames(app);

-- external content：索引指向 frames.text，不再存第二份正文
CREATE VIRTUAL TABLE IF NOT EXISTS fts_tri USING fts5(
    text, content='frames', content_rowid='id', tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _parts(stem: str) -> tuple[str, str]:
    """10-43-13-s2 → ('10-43-13', 's2')；无后缀的是前台屏。"""
    bits = stem.split("-")
    return ("-".join(bits[:3]), bits[3] if len(bits) > 3 else "active")


def index_frame(db: sqlite3.Connection, day: str, stem: str,
                app: str, title: str, text: str, source: str) -> None:
    """写入或更新一帧。重复调用是幂等的。"""
    ts, display = _parts(stem)
    cur = db.execute(
        "SELECT id FROM frames WHERE day=? AND ts=? AND display=?", (day, ts, display)
    ).fetchone()
    if cur:
        rid = cur["id"]
        # external content 模式下改正文要先把旧索引项撤掉，用 'delete' 指令
        old = db.execute("SELECT text FROM frames WHERE id=?", (rid,)).fetchone()["text"]
        db.execute("INSERT INTO fts_tri(fts_tri, rowid, text) VALUES('delete', ?, ?)",
                   (rid, old))
        db.execute("UPDATE frames SET app=?,title=?,source=?,chars=?,text=? WHERE id=?",
                   (app, title, source, len(text), text, rid))
    else:
        rid = db.execute(
            "INSERT INTO frames(day,ts,display,app,title,source,chars,text)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (day, ts, display, app, title, source, len(text), text),
        ).lastrowid
    db.execute("INSERT INTO fts_tri(rowid, text) VALUES(?,?)", (rid, text))


def forget_day(db: sqlite3.Connection, day: str) -> int:
    """删掉某一天的索引。数据删了索引必须跟着删，否则搜出来的是幽灵。"""
    rows = db.execute("SELECT id, text FROM frames WHERE day=?", (day,)).fetchall()
    for r in rows:
        db.execute("INSERT INTO fts_tri(fts_tri, rowid, text) VALUES('delete', ?, ?)",
                   (r["id"], r["text"]))
    db.execute("DELETE FROM frames WHERE day=?", (day,))
    return len(rows)


def _quote(q: str) -> str:
    """FTS5 的查询语法会把标点当运算符，整个包成短语避免语法错误。"""
    return '"' + q.replace('"', '""') + '"'


def search(db: sqlite3.Connection, keyword: str, days: list[str] | None = None,
           apps_exclude: list[str] | None = None, limit: int = 30) -> list[sqlite3.Row]:
    """按关键词搜。3 字符以上走 FTS 索引，2 字符退回 LIKE 全表扫。"""
    if len(keyword) >= 3:
        sql = ["SELECT f.* FROM fts_tri JOIN frames f ON f.id = fts_tri.rowid",
               "WHERE fts_tri MATCH ?"]
        args: list = [_quote(keyword)]
    else:
        # trigram 够不着 2 字符，unicode61 又不切中文，只能扫
        sql = ["SELECT f.* FROM frames f WHERE f.text LIKE ?"]
        args = [f"%{keyword}%"]
    if days:
        sql.append(f"AND f.day IN ({','.join('?' * len(days))})")
        args += days
    for app in apps_exclude or []:
        sql.append("AND (f.app IS NULL OR lower(f.app) NOT LIKE ?)")
        args.append(f"%{app.lower()}%")
    sql.append("ORDER BY f.day DESC, f.ts DESC LIMIT ?")
    args.append(limit)
    return db.execute(" ".join(sql), args).fetchall()


def count_excluded(db: sqlite3.Connection, keyword: str,
                   days: list[str] | None, apps: list[str]) -> int:
    """命中里有多少因为「仅本地应用」被挡掉 —— 挡了多少必须能说出来。"""
    if not apps:
        return 0
    total = len(search(db, keyword, days=days, limit=10_000))
    kept = len(search(db, keyword, days=days, apps_exclude=apps, limit=10_000))
    return total - kept


def stats(db: sqlite3.Connection) -> dict:
    row = db.execute(
        "SELECT count(*) n, count(DISTINCT day) days, sum(chars) chars FROM frames"
    ).fetchone()
    return {"frames": row["n"] or 0, "days": row["days"] or 0, "chars": row["chars"] or 0}


# ==================== 从文件重建 ====================

_AX_TRUST_CHARS = 1000


def _read_meta(base: pathlib.Path):
    try:
        lines = base.with_suffix(".meta").read_text(encoding="utf-8").strip().splitlines()
    except (FileNotFoundError, OSError):
        return "", ""
    app = lines[0] if lines else ""
    if app.startswith("pid="):
        app = ""
    return app, (lines[1] if len(lines) > 1 else "")


def _read_text(base: pathlib.Path) -> tuple[str, str]:
    """索引这一帧的全部文本：无障碍树和 OCR 都要。

    analyze.py 是「谁多用谁」二选一 —— 那是为了不让同一屏的内容在模型
    上下文里出现两遍。检索不能照抄这条规则：两份是同一块屏的不同视角，
    谁都可能有你要找的词。实测有一帧 .txt 里有「订座」而 .ocr 里没有，
    按字数只选了 .ocr，那一帧就从搜索里消失了。

    按行去重，因为两份重叠的部分很多。
    """
    texts, sources = [], []
    for ext, name in ((".txt", "accessibility"), (".ocr", "ocr")):
        try:
            t = base.with_suffix(ext).read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        if t:
            texts.append(t)
            sources.append(name)
    if not texts:
        return "", "none"

    seen: set[str] = set()
    lines = []
    for t in texts:
        for line in t.splitlines():
            k = line.strip()
            if k and k not in seen:
                seen.add(k)
                lines.append(line)
    return "\n".join(lines), "+".join(sources)


def rebuild(screenshot_dir: pathlib.Path, db=None, verbose: bool = True) -> dict:
    """从 screenshots/ 全量重建索引。

    这个函数存在本身就是设计的一部分：索引随时可以丢，文件才是根。
    """
    db = db or connect()
    db.execute("DELETE FROM frames")
    db.execute("INSERT INTO fts_tri(fts_tri) VALUES('rebuild')")
    n = 0
    for day_dir in sorted(d for d in screenshot_dir.iterdir() if d.is_dir()):
        seen: set[str] = set()
        for f in sorted(day_dir.iterdir()):
            if f.suffix not in (".meta", ".txt", ".ocr"):
                continue
            if f.stem in seen:
                continue
            seen.add(f.stem)
            base = day_dir / f.stem
            app, title = _read_meta(base)
            text, source = _read_text(base)
            index_frame(db, day_dir.name, f.stem, app, title, text, source)
            n += 1
        if verbose:
            print(f"  {day_dir.name}: {len(seen)} 帧")
    db.commit()
    db.execute("INSERT INTO fts_tri(fts_tri) VALUES('optimize')")
    db.commit()
    return {"frames": n}


def _main() -> None:
    import sys
    import time
    import yaml
    cfg = yaml.safe_load((SCRIPT_DIR / "config.yaml").read_text())
    shots = SCRIPT_DIR / cfg["capture"]["output_dir"]
    t = time.time()
    print(f"从 {shots} 重建索引…")
    r = rebuild(shots)
    db = connect()
    print(f"完成 {r['frames']} 帧，耗时 {time.time() - t:.1f}s")
    print("统计:", stats(db))
    print(f"索引体积: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    _main()
