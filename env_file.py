"""从 .env 读凭证注入进程环境。

为什么需要它：launchd 启动的进程不继承登录 shell 的环境变量，
拿不到 ~/.zshrc 里导出的 API key。补救办法有两个 ——
把 key 写进 LaunchAgent plist 的 EnvironmentVariables，或者让程序
自己读 .env。前者会把明文密钥留在一个迟早要进版本库的模板文件里，
所以选后者：plist 只描述怎么跑，凭证只存在于被 .gitignore 的 .env。

已存在的环境变量优先级更高，.env 不覆盖它 —— 这样临时用
`ZHIPU_API_KEY=xxx python analyze.py` 覆盖一次仍然有效。
"""

import os
from pathlib import Path


def load(env_path: Path | None = None) -> None:
    """把 .env 里的键值注入 os.environ。文件不存在就静默跳过。"""
    path = env_path or Path(__file__).resolve().parent / ".env"
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # 去掉可能存在的引号包裹
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
