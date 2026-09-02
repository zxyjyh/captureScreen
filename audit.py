"""删除审计。

写这个是因为出过一次说不清的事：某天 09-01 的图片全没了，
定时任务的条件算下来不该删，看板的删除又不留痕，
最后只能靠「残留文件的形态像哪个按钮干的」去推测。

一个会删用户数据的工具，必须能回答「谁、什么时候、删了什么」。
日志只追加不覆盖，且和被删的数据分开存 —— 删数据的时候不能把
记录一起删掉。
"""

import os
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "deletions.log"


def record(source: str, what: str, count: int = 0, bytes_freed: int = 0) -> None:
    """记一条删除。source 是发起方：定时任务 / 看板 / 命令行。"""
    stamp = datetime.now().isoformat(timespec="seconds")
    size = f"{bytes_freed / 1024 / 1024:.1f}MB" if bytes_freed else "-"
    line = f"{stamp}\t{source}\t{what}\t{count}项\t{size}\n"
    try:
        umask = os.umask(0o077)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        finally:
            os.umask(umask)
    except OSError:
        pass  # 记不上也不能拖垮删除本身


def tail(n: int = 50) -> list[str]:
    try:
        return LOG_FILE.read_text(encoding="utf-8").splitlines()[-n:]
    except (FileNotFoundError, OSError):
        return []
