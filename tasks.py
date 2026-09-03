"""按任务和工作性质统计时间。

按应用统计回答不了「这周花在订座系统上多少时间」—— 同一个 VS Code
可能在改三个项目，同一个 Chrome 可能在查文档也可能在看闲书。

两个维度是正交的：
  任务：在做哪个项目 —— 靠关键词匹配窗口标题和屏幕正文
  性质：在做哪类事   —— 靠应用归类（编码 / 沟通 / 阅读 / 写作）

为什么用关键词而不是让模型判：一是每小时一次调用不便宜，二是
跨小时的判定会飘 —— 同一个项目这小时叫「订座系统」下小时叫
「餐厅预订」，统计就没法加总。关键词是确定的、可复算的，
而且只有你自己知道哪些东西该算作「一项任务」。

配置在 tasks.local.yaml，已被 .gitignore —— 里面是真实项目名。
"""

import collections
import re
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "tasks.local.yaml"

# 应用 → 工作性质。这份映射是通用的，不像任务那样因人而异，
# 所以放代码里给个默认，配置里可以覆盖和补充。
DEFAULT_KINDS = {
    "编码": ["code", "vscode", "cursor", "ghostty", "iterm", "terminal", "xcode",
             "intellij", "pycharm", "webstorm", "goland", "sublime", "zed"],
    "沟通": ["钉钉", "dingtalk", "slack", "飞书", "lark", "wechat", "微信",
             "mail", "邮件", "teams", "zoom", "腾讯会议"],
    "阅读": ["chrome", "safari", "firefox", "edge", "arc", "preview", "预览",
             "books", "reader"],
    "写作": ["typora", "notion", "obsidian", "word", "pages", "备忘录", "notes"],
    "设计": ["figma", "sketch", "摹客", "photoshop", "principle"],
    "其他": [],
}


def load() -> dict:
    try:
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return {"tasks": {}, "kinds": DEFAULT_KINDS}
    kinds = dict(DEFAULT_KINDS)
    for k, v in (data.get("kinds") or {}).items():
        kinds[k] = [str(x) for x in (v or [])]
    # 关键词一律转成字符串：YAML 会把需求号 3718 解析成整数，
    # 到匹配那一步才炸，且报错离配置很远
    tasks = {k: [str(x) for x in (v or [])]
             for k, v in (data.get("tasks") or {}).items()}
    return {"tasks": tasks, "kinds": kinds}


def _score(text: str, keywords: list[str]) -> int:
    t = (text or "").lower()
    return sum(t.count(w.lower()) for w in keywords)


def attribute(title: str, text: str, app: str, cfg: dict | None = None) -> tuple[str, str]:
    """判定一帧属于哪个任务、哪类工作。返回 (任务, 性质)。

    标题优先于正文：VS Code 的标题直接带仓库名（`xxx.java — 项目名 — vscode`），
    是最强的信号；正文里别的项目名可能只是被顺带提到。
    标题定不了才按正文里关键词出现的次数比大小。
    """
    cfg = cfg or load()
    tasks = cfg["tasks"]

    task = "未归类"
    if tasks:
        by_title = {k: _score(title, kws) for k, kws in tasks.items()}
        if sum(by_title.values()):
            task = max(by_title, key=by_title.get)
        else:
            by_text = {k: _score(text, kws) for k, kws in tasks.items()}
            if sum(by_text.values()):
                task = max(by_text, key=by_text.get)

    kind = "其他"
    low = (app or "").lower()
    for name, apps in cfg["kinds"].items():
        if any(a.lower() in low for a in apps):
            kind = name
            break
    return task, kind


def allocate(frames: list[dict], cfg: dict | None = None) -> tuple[dict, dict]:
    """按任务和性质分别统计分钟数。

    frames 需按时间排序，每项含 ts / app / title / text。
    时长取相邻两帧的间隔，最后一帧按最小间隔算 —— 和 build_time_allocation
    同一套口径，免得两处数字对不上。
    """
    cfg = cfg or load()
    by_task: collections.Counter = collections.Counter()
    by_kind: collections.Counter = collections.Counter()

    for i, f in enumerate(frames):
        if i + 1 < len(frames):
            gap = _minutes(frames[i + 1]["ts"]) - _minutes(f["ts"])
        else:
            gap = 1
        gap = max(1, min(gap, 15))  # 单帧最多算 15 分钟，避免长空档被算成工作
        task, kind = attribute(f.get("title", ""), f.get("text", ""), f.get("app", ""), cfg)
        by_task[task] += gap
        by_kind[kind] += gap
    return dict(by_task), dict(by_kind)


def _minutes(ts: str) -> int:
    m = re.match(r"(\d{2})-(\d{2})", ts or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


# ==================== 从数据里推荐任务 ====================

def suggest(limit: int = 20) -> list[tuple[str, int]]:
    """从窗口标题里挖候选任务名。

    标题里的仓库名、项目名是最干净的来源 —— VS Code 和 GitLab 的标题
    结构固定，比在正文里瞎猜靠谱。挖出来给人确认，不自动写进配置。
    """
    import store
    db = store.connect()
    rows = db.execute("SELECT title FROM frames WHERE title != ''").fetchall()
    db.close()

    # 应用名、系统窗口、以及一眼就是文件名的，都不是任务
    APP_NOISE = {"vscode", "google chrome", "chrome", "safari", "firefox",
                 "loginwindow", "finder", "访达", "钉钉", "dingtalk", "ghostty",
                 "typora", "code", "terminal", "window"}
    FILE_EXT = re.compile(r"\.(py|js|ts|tsx|vue|java|sql|ya?ml|json|md|css|html?|"
                          r"go|rs|sh|xml|properties|gitignore|txt|log)$", re.I)

    hits: collections.Counter = collections.Counter()
    for r in rows:
        t = r["title"]
        parts = []
        # VS Code: `文件 — 仓库名 — vscode`；GitLab: `页面 · 项目 · 分组`
        for sep in (r"\s+[—–]\s+", r"\s+·\s+", r"\s+/\s+"):
            if re.search(sep, t):
                parts += [x.strip() for x in re.split(sep, t)]
        for part in parts:
            low = part.lower()
            if not (3 <= len(part) <= 40):
                continue
            if low in APP_NOISE or FILE_EXT.search(part):
                continue
            # 还带分隔符说明没切干净，跳过
            if re.search(r"[—–·]|\s-\s", part):
                continue
            hits[part] += 1
    return [(w, c) for w, c in hits.most_common(limit * 3) if c >= 3][:limit]


def _main() -> None:
    cfg = load()
    if not cfg["tasks"]:
        print(f"还没配置任务。复制模板再填：\n"
              f"  cp {SCRIPT_DIR / 'tasks.example.yaml'} {CONFIG_FILE}\n")
    print("从窗口标题里挖到的候选（出现 >= 3 次）：\n")
    for name, count in suggest():
        print(f"  {count:4d}  {name}")
    print("\n把同一个项目的不同写法归到一个任务名下，填进 tasks.local.yaml。")


if __name__ == "__main__":
    _main()
