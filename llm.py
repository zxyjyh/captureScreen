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
