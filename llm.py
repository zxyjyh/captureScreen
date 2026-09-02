"""智谱开放平台的最小客户端。

为什么不用官方 zhipuai SDK：它把 pyjwt 钉在 <2.9.0，而 mcp 要求 >=2.10.1，
两者在同一个 venv 里装不下。SDK 在这个项目里只做两件事 —— 拼 HTTP 请求、
带上 Bearer token，都不值得为它牺牲整条 MCP 链路。
httpx 本来就是 mcp 的依赖，改用它反而少一个直接依赖。

刻意保持与 SDK 相同的调用形状（client.chat.completions.create / .embeddings.create），
这样调用点只需要改 import 那一行。
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
TIMEOUT = 120.0
MAX_RETRIES = 3
# 这些是「等一会儿再试就好」的错误：限流、网关抖动。
# 余额不足（1113）和鉴权失败不在此列 —— 重试多少次都一样。
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """带上智谱返回的 errcode，方便上层区分「该重试」和「该停手」。"""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class _Message:
    content: str


@dataclass
class _Choice:
    message: _Message


@dataclass
class _ChatResponse:
    choices: list[_Choice]
    usage: dict[str, Any] | None = None


@dataclass
class _EmbeddingItem:
    embedding: list[float]


@dataclass
class _EmbeddingResponse:
    data: list[_EmbeddingItem]


_redactor = None


def redactor():
    """全局脱敏器，懒加载。术语表在进程生命周期内只读一次。"""
    global _redactor
    if _redactor is None:
        import redact
        _redactor = redact.Redactor(redact.load_terms())
    return _redactor


def _scrub(payload: dict) -> dict:
    """出网前脱敏。

    放在这一层而不是各调用点：调用点有七个（分析、日报、周期汇总、
    问答、向量索引……），漏掉任何一个就前功尽弃 ——
    实测就漏过：报告在 analyze.py 里脱敏后还原成真名，
    转手又被 rag.py 发去做 embedding，绕过了整套机制。
    收口在这里，新增调用点也自动被覆盖。

    只处理文本字段。图片是 base64，既扫不动也没法脱敏 ——
    多模态路径只能靠 privacy.local_only_apps 挡在前面。
    """
    r = redactor()
    if not r.term_count:
        # 术语表为空时仍要跑模式匹配，邮箱手机号不该出网
        pass

    def scrub_text(v):
        return r.redact(v) if isinstance(v, str) else v

    out = dict(payload)
    if isinstance(out.get("messages"), list):
        msgs = []
        for m in out["messages"]:
            m = dict(m)
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = scrub_text(c)
            elif isinstance(c, list):
                m["content"] = [
                    {**part, "text": scrub_text(part["text"])}
                    if isinstance(part, dict) and part.get("type") == "text" and "text" in part
                    else part
                    for part in c
                ]
            msgs.append(m)
        out["messages"] = msgs

    inp = out.get("input")
    if isinstance(inp, str):
        out["input"] = scrub_text(inp)
    elif isinstance(inp, list):
        out["input"] = [scrub_text(x) for x in inp]

    return out


def _post(path: str, payload: dict, api_key: str) -> dict:
    if not api_key:
        raise LLMError("未配置 API key：把 ZHIPU_API_KEY 写进 .env")

    payload = _scrub(payload)
    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        except httpx.RequestError as e:
            last = LLMError(f"网络请求失败: {e}")
            time.sleep(2**attempt)
            continue

        if r.status_code == 200:
            body = r.json()
            # 智谱有些错误是 HTTP 200 + body 里带 error，不看 body 会当成成功
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                raise LLMError(f"接口返回错误: {err}", code=str(err.get("code", "")))
            return body

        code, detail = "", r.text[:300]
        try:
            err = r.json().get("error", {})
            code, detail = str(err.get("code", "")), err.get("message", detail)
        except (json.JSONDecodeError, AttributeError):
            pass

        if r.status_code in RETRYABLE_STATUS and code != "1113" and attempt < MAX_RETRIES - 1:
            last = LLMError(detail, status=r.status_code, code=code)
            time.sleep(2**attempt)
            continue

        hint = "（余额不足，去控制台充值）" if code == "1113" else ""
        raise LLMError(f"HTTP {r.status_code} code={code}: {detail}{hint}",
                       status=r.status_code, code=code)

    raise last or LLMError("重试耗尽")


# ==================== Claude 后端 ====================

class _ClaudeCompletions:
    """走 claude-agent-sdk。

    选它而不是 anthropic SDK 的唯一理由是鉴权：SDK 透过 Claude Code CLI
    用现有订阅，不需要单独的 API key，也就没有「余额不足」这回事 ——
    智谱那边正是卡在这里。代价是每次调用要起一个 CLI 子进程，约 9 秒。

    这里不需要 agent 能力（没有工具、没有多轮），所以 allowed_tools 清空、
    max_turns=1，把它当成一次性的补全接口用。
    """

    def __init__(self, model: str = ""):
        self._model = model

    def create(self, *, model: str, messages: list[dict], **kwargs) -> _ChatResponse:
        import anyio
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, TextBlock, query,
        )

        payload = _scrub({"messages": messages})
        system, parts = "", []
        for m in payload["messages"]:
            content = m.get("content")
            if isinstance(content, list):
                # 多模态：这条路径本该被 local_only_apps 挡住，
                # 真走到这里也只取文本，绝不把图片交出去
                content = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if m.get("role") == "system":
                system = content or ""
            else:
                parts.append(content or "")

        prompt = "\n\n".join(p for p in parts if p)

        async def run() -> str:
            opts = ClaudeAgentOptions(
                system_prompt=system or None,
                allowed_tools=[],
                max_turns=1,
                model=model or self._model or None,
            )
            out = []
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            out.append(block.text)
            return "".join(out)

        try:
            text = anyio.run(run)
        except Exception as e:
            raise LLMError(f"claude-agent-sdk 调用失败: {e}") from e
        if not text.strip():
            raise LLMError("claude-agent-sdk 返回空内容")
        return _ChatResponse(choices=[_Choice(_Message(text))])


class _ClaudeChat:
    def __init__(self, model: str = ""):
        self.completions = _ClaudeCompletions(model)


class _LocalEmbeddings:
    """本地向量化。

    Anthropic 没有 embedding 接口。与其为此单独保留一个第三方 API，
    不如用 chromadb 自带的本地模型 —— 免费、离线，而且直接少掉一条出网路径，
    屏幕内容不必再为了检索被送出去。
    """

    _fn = None

    def create(self, *, model: str, input: list[str] | str) -> _EmbeddingResponse:
        if _LocalEmbeddings._fn is None:
            from chromadb.utils import embedding_functions
            _LocalEmbeddings._fn = embedding_functions.DefaultEmbeddingFunction()
        texts = [input] if isinstance(input, str) else list(input)
        # 本地模型不出网，但仍然脱敏：向量库落在磁盘上，
        # 人名进了索引一样是留痕
        r = redactor()
        vectors = _LocalEmbeddings._fn([r.redact(t) for t in texts])
        # 本地模型返回 numpy float32，chromadb 的 upsert 只收 Python float
        return _EmbeddingResponse(
            [_EmbeddingItem([float(x) for x in v]) for v in vectors]
        )


class ClaudeClient:
    def __init__(self, api_key: str = "", model: str = ""):
        self.chat = _ClaudeChat(model)
        self.embeddings = _LocalEmbeddings()


def get_client(provider: str = "claude", api_key: str = "", model: str = ""):
    """按配置返回客户端。两个后端的调用形状一致。"""
    if provider == "zhipu":
        return ZhipuClient(api_key)
    return ClaudeClient(api_key, model)


class _Completions:
    def __init__(self, key: str):
        self._key = key

    def create(self, *, model: str, messages: list[dict], **kwargs) -> _ChatResponse:
        body = _post("/chat/completions", {"model": model, "messages": messages, **kwargs}, self._key)
        choices = [
            _Choice(_Message(c.get("message", {}).get("content") or ""))
            for c in body.get("choices", [])
        ]
        if not choices:
            raise LLMError(f"返回中没有 choices: {str(body)[:200]}")
        return _ChatResponse(choices=choices, usage=body.get("usage"))


class _Chat:
    def __init__(self, key: str):
        self.completions = _Completions(key)


class _Embeddings:
    def __init__(self, key: str):
        self._key = key

    def create(self, *, model: str, input: list[str] | str) -> _EmbeddingResponse:
        body = _post("/embeddings", {"model": model, "input": input}, self._key)
        return _EmbeddingResponse([_EmbeddingItem(d["embedding"]) for d in body.get("data", [])])


class ZhipuClient:
    def __init__(self, api_key: str = ""):
        key = api_key or os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
        self.chat = _Chat(key)
        self.embeddings = _Embeddings(key)
