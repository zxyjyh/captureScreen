"""从屏幕文本里挖术语，用来纠正语音转写。

Whisper 听中文句子没问题，专有名词几乎全错 —— 实测「订座」听成「定做」、
「Navicat」听成「nevicot」、「ENABLED」听成「enappled」。这不是模型小，
是它没见过你的项目词汇，任何尺寸的 Whisper 都一样。

而这些词的正确写法，屏幕上到处都是。把它们喂进 initial_prompt，
就是拿「看到的」去纠正「听到的」。

选词的关键不是词频 —— 全局最高频的是 tab、mode、Close 这类界面装饰。
真正的术语只在少数帧里密集出现，所以用 tf-idf：总频次高、文档频率低。
再按时间窗口收窄：会议在某个时段发生，那个时段屏幕上的词才相关。
"""

import collections
import math
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}")
# 中文术语：2-6 字，两侧不是中文（避免从长句中间乱切）
_CJK = re.compile(r"[一-鿿]{2,24}")

# 常见英文词和界面词。它们词频高但没有纠正价值，
# 占掉 initial_prompt 有限的额度就是浪费。
_STOP = set("""
the and for you are was not with this that from have has had will would can could
your our their its been being does did but all any out get got make made use used
new old more most some such only other than then them they there here when what
which who how why into over under about after before during while each both few
http https com www org net html json yaml png jpg jpeg txt svg css scss
div span class style href src alt img button input form table
function return const let var import export default async await null true false
undefined int str list dict def self print open read write path file name value
tab mode close bar shift current main master menu window view page item list
edit view help file save open close new delete search find select cancel
google chrome code finder about blank index terminal
""".split())


# 浏览器和系统的界面文案。它们在屏幕上到处都是，但**没人会在会议里说出来**，
# 却会吃掉 initial_prompt 仅有的额度。
#
# 试过用数据本身识别（窗口标题分散度、应用分散度），不成立 ——
# 「澳觅」是真术语却出现在 20 个标题 / 7 个应用里，比界面词「想要访问」
# 的 14/2 还分散。所以这里就是一份显式清单，换浏览器或换界面语言要补。
_CJK_CHROME = (
    "想要访问此网站 可以访问此网站 想要访问 可以访问 访问此网站 此网站 标签页 新标签页 地址和搜索栏 重新加载 "
    "查看网站信息 为此标签页 添加书签 前进 后退 关闭标签 打开新的 隐身 无痕 "
    "文件 编辑 显示 历史记录 书签 个人资料 窗口 帮助 视图 工具 设置 偏好设置 "
    "全部标签页 下载内容 扩展程序 更多工具 页面另存为 打印 投放 翻译此页 "
    "未读 已读 撤回 转发 收藏 表情 语音 视频通话 群聊 消息 通知 待办 日程 "
    "复制 粘贴 剪切 撤销 重做 全选 查找 替换 保存 另存为 退出 关于"
).split()


def _is_chrome(w: str) -> bool:
    """整词命中，或本身就是某条界面文案的片段 —— 滑窗必然切出片段。"""
    return any(w == c or (len(w) >= 2 and w in c) for c in _CJK_CHROME)


def _cjk_terms(text: str) -> collections.Counter:
    """中文没有分词器，退一步用 2-4 字的滑窗，靠 tf-idf 筛掉噪声。

    这比接 jieba 粗糙，但省一个依赖，而且目标只是「给 Whisper 一份提示」——
    多几个噪声词的代价远小于漏掉「订座」。
    """
    out = collections.Counter()
    for run in _CJK.findall(text):
        # 上到 6 字：4 字封顶时「香港预约订位」永远切不出整词，
        # 只剩「香港预约」「港预约订」「预约订位」互相竞争，
        # 极大子串去重也就无从下手
        for n in range(2, 7):
            for i in range(len(run) - n + 1):
                out[run[i:i + n]] += 1
    return out


def extract(rows: list[str], background: list[str] | None = None,
            limit: int = 40, min_count: int = 3) -> list[str]:
    """从若干帧的文本里挑出术语。background 用来算文档频率。"""
    corpus = background if background is not None else rows
    n_docs = max(len(corpus), 1)

    tf: collections.Counter = collections.Counter()
    for t in rows:
        tf.update(w for w in _WORD.findall(t) if w.lower() not in _STOP)
        tf.update({w: c for w, c in _cjk_terms(t).items() if not _is_chrome(w)})

    df: collections.Counter = collections.Counter()
    for t in corpus:
        seen = {w for w in _WORD.findall(t) if w.lower() not in _STOP}
        seen |= {w for w in _cjk_terms(t) if not _is_chrome(w)}
        df.update(seen)

    scored = []
    for w, c in tf.items():
        if c < min_count:
            continue
        d = df.get(w, 1)
        # 铺满帧的是界面装饰（Chrome 的「想要访问此网站」这类），压下去
        if d / n_docs > 0.3:
            continue
        scored.append((c * math.log(n_docs / d), w))
    scored.sort(reverse=True)

    # 滑窗切出来的中文必然重叠：「项目」「项目分」「目分析」「项目分析」
    # 会同时上榜，把有限的 prompt 额度吃光。
    # 只留极大子串：一个词如果被另一个候选包含、且出现次数接近
    # （说明它多半只是那个长词的片段），就丢掉。
    cands = [w for _, w in scored[:limit * 6]]
    picked: list[str] = []
    for w in cands:
        if any(w != o and w in o and tf[o] >= tf[w] * 0.7 for o in cands):
            continue
        picked.append(w)
        if len(picked) >= limit:
            break
    return picked


def near(day: str, hour: int | None = None, window: int = 2, limit: int = 40) -> list[str]:
    """取某个时段前后屏幕上的术语。hour 为空则取一整天。"""
    import store
    db = store.connect()
    if hour is None:
        rows = [r["text"] for r in db.execute(
            "SELECT text FROM frames WHERE day=? AND text!=''", (day,))]
    else:
        lo, hi = f"{max(0, hour - window):02d}", f"{min(23, hour + window):02d}"
        rows = [r["text"] for r in db.execute(
            "SELECT text FROM frames WHERE day=? AND substr(ts,1,2) BETWEEN ? AND ?"
            " AND text!=''", (day, lo, hi))]
    background = [r["text"] for r in db.execute(
        "SELECT text FROM frames WHERE text!=''")]
    db.close()
    return extract(rows, background, limit=limit)


def as_prompt(terms: list[str], max_chars: int = 220) -> str:
    """拼成 initial_prompt。Whisper 只看约 224 个 token，超了直接截断，
    所以按重要性排序之后从前往后塞，塞满为止。"""
    out: list[str] = []
    total = 0
    for t in terms:
        if total + len(t) + 1 > max_chars:
            break
        out.append(t)
        total += len(t) + 1
    return "、".join(out)
