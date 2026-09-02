"""发给模型之前把人名、公司名换成占位符，收到回答之后换回来。

为什么不靠模型或 NER 来识别：那需要先把原文发出去，本末倒置。
所以走术语表 —— 由使用者自己维护，确定性、可审计、零推理成本。
代价是要手动补充，但同事和公司名是个有界集合，补几次就稳定了。

术语表存在 redact.local.yaml，已被 .gitignore ——
把真实同事姓名提交进版本库，正是这个模块要防的事。

两类替换，处理方式不同：
  术语表里的     替换成【人名1】这类带序号的占位符，报告生成后换回来。
                 带序号是为了让模型仍能分辨「谁对谁说」。
  邮箱/电话/长数字 直接换成【邮箱】这类通用标记，不换回来 ——
                 它们对「我那天在做什么」没有信息量，还原只是徒增风险。
"""

import re
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
TERMS_FILE = SCRIPT_DIR / "redact.local.yaml"

# 这些无差别抹掉，不还原
_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "【邮箱】"),
    (re.compile(r"(?<!\d)(?:\+?853[-\s]?)?\d{4}[-\s]?\d{4}(?!\d)"), "【电话】"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "【电话】"),
    (re.compile(r"(?<!\d)\d{15,20}(?!\d)"), "【长串数字】"),
]


def load_terms(path: Path | None = None) -> dict[str, list[str]]:
    """读术语表。没有文件就返回空表，脱敏退化成只做模式匹配。"""
    path = path or TERMS_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return {}
    return {k: [str(t) for t in (v or []) if str(t).strip()] for k, v in data.items()}


class Redactor:
    """一次分析用一个实例：替换与还原共用同一份映射。"""

    def __init__(self, terms: dict[str, list[str]] | None = None):
        self._map: dict[str, str] = {}   # 原文 -> 占位符
        self._back: dict[str, str] = {}  # 占位符 -> 原文
        for label, items in (terms or {}).items():
            # 长的先替换：否则「澳觅」会把「澳觅科技」切成两半
            for i, term in enumerate(sorted(set(items), key=len, reverse=True), start=1):
                token = f"【{label}{i}】"
                self._map[term] = token
                self._back[token] = term

    def redact(self, text: str) -> str:
        for term, token in self._map.items():
            text = text.replace(term, token)
        for pattern, token in _PATTERNS:
            text = pattern.sub(token, text)
        return text

    def restore(self, text: str) -> str:
        """把占位符换回原文。模型没原样吐回来的占位符会留在原地 ——
        那是安全的一侧：宁可报告里留个【人名3】，也不要错还原成别人。"""
        for token, term in self._back.items():
            text = text.replace(token, term)
        return text

    @property
    def term_count(self) -> int:
        return len(self._map)


# ==================== 候选发现 ====================

# 聊天界面里人名出现的两种结构位置：@提及、和「发言人:」前缀。
# 靠结构而不是靠字形，所以繁体简体都能捞到 —— 屏幕上是什么样就捞到什么样，
# 不需要繁简转换表。
_MENTION = re.compile(r"@([\u4e00-\u9fff]{2,4})")
_SPEAKER = re.compile(r"^([\u4e00-\u9fff]{2,4})\s*[:：]", re.MULTILINE)

# 这些结构上像发言人、实际不是人名
_NOT_NAMES = {
    "工作通知", "系统通知", "告警消息", "日程", "未读", "所有人", "群通知",
    "会话", "文件", "图片", "语音", "视频", "位置", "链接", "转账",
    "草稿", "撤回", "已读", "回复", "转发", "提醒", "待办", "审批",
}


def discover(texts, min_count: int = 2) -> list[tuple[str, int]]:
    """从屏幕文本里找出可能是人名的词，按出现次数排序。

    只做建议，不自动脱敏 —— 结构匹配一定有误报，
    把「工作通知」当人名替换掉会让报告变得没法读。
    由人过一遍再决定加不加，代价很低（这个集合是有界的，补几次就稳了）。
    """
    from collections import Counter
    hits: Counter = Counter()
    for text in texts:
        for pattern in (_MENTION, _SPEAKER):
            for name in pattern.findall(text):
                if name not in _NOT_NAMES:
                    hits[name] += 1
    return [(n, c) for n, c in hits.most_common() if c >= min_count]


def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="从本地屏幕文本里发现候选人名")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--min-count", type=int, default=2)
    args = parser.parse_args()

    shots = SCRIPT_DIR / "screenshots"
    texts = []
    for f in list(shots.rglob("*.txt")) + list(shots.rglob("*.ocr")):
        try:
            texts.append(f.read_text(encoding="utf-8"))
        except OSError:
            continue

    known = {t for v in load_terms().values() for t in v}
    found = discover(texts, args.min_count)
    new = [(n, c) for n, c in found if n not in known]

    print(f"扫描 {len(texts)} 份本地文本，已在术语表中 {len(known)} 个词")
    if not new:
        print("没有发现新的候选。")
        return
    print(f"\n发现 {len(new)} 个候选（出现 >= {args.min_count} 次）。")
    print("人过一遍，把真的是人名的粘进 redact.local.yaml 的「人名」下：\n")
    for name, count in new:
        print(f"  - {name}    # 出现 {count} 次")
    print("\n注意：这里只按「@提及」和「发言人:」的结构匹配，一定有误报。")


if __name__ == "__main__":
    _main()
