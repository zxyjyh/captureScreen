"""RAG 核心模块 - 报告索引与语义检索"""

import os
import re
from pathlib import Path

import chromadb
import yaml
from zhipuai import ZhipuAI

SCRIPT_DIR = Path(__file__).resolve().parent

_COLLECTION_NAME = "screen_reports"


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def _get_client() -> ZhipuAI:
    api_key = os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
    return ZhipuAI(api_key=api_key)


def _get_collection(rag_config: dict) -> chromadb.Collection:
    db_path = SCRIPT_DIR / rag_config["db_path"]
    client = chromadb.PersistentClient(path=str(db_path))
    return client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _embed(texts: list[str], model: str) -> list[list[float]]:
    client = _get_client()
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


# 报告小节 -> chunk 归属。未列出的小节不索引。
# 「时间分配」是纯统计，检索价值为零；「亮点」「可改进」是模型的泛化说教，
# 按项目目标（找回 / 重建 / 合成）属于非目标内容，索引它们只会稀释检索结果。
_SECTION_ROUTING = {
    "活动时间线": "活动事实",
    "活动流": "活动事实",
    "具体做了什么": "活动事实",  # 兼容早期报告格式
    "关键内容": "关键内容",
    "查看的内容": "关键内容",    # 兼容早期报告格式
    "意图分析": "关键内容",
}

_CHUNK_ORDER = ["活动事实", "关键内容"]


def _split_sections(content: str) -> list[tuple[str, str]]:
    """按 ## / ### 标题拆出 (标题, 正文) 列表。

    报告里 `## 具体内容` 只是个容器，真正的内容在它下面的 ### 子标题里，
    所以两级标题一视同仁地拆平。
    """
    parts = re.split(r"^#{2,3} (.+)$", content, flags=re.MULTILINE)
    sections = []
    for i in range(1, len(parts), 2):
        header = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            sections.append((header, body))
    return sections


def _chunk_report(content: str) -> list[tuple[str, str]]:
    """将报告合并为 2 个 chunk（活动事实 + 关键内容），减少 embedding API 调用"""
    buckets: dict[str, list[str]] = {}

    for header, body in _split_sections(content):
        bucket = _SECTION_ROUTING.get(header)
        if bucket is None:
            continue
        buckets.setdefault(bucket, []).append(f"## {header}\n{body}")

    return [
        (name, "\n\n".join(buckets[name]))
        for name in _CHUNK_ORDER
        if buckets.get(name)
    ]


def index_report(date: str, hour: int, content: str):
    """将一份小时报告分块并索引到 ChromaDB"""
    config = load_config()
    rag_config = config["rag"]
    collection = _get_collection(rag_config)
    embedding_model = rag_config["embedding_model"]

    chunks = _chunk_report(content)
    if not chunks:
        return

    ids = []
    documents = []
    metadatas = []

    for section_name, text in chunks:
        chunk_id = f"{date}-{hour:02d}-{section_name}"
        ids.append(chunk_id)
        documents.append(text)
        metadatas.append({"date": date, "hour": str(hour), "section": section_name})

    embeddings = _embed(documents, embedding_model)

    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    print(f"Indexed {len(chunks)} chunks for {date} {hour:02d}:00")


def search(
    query: str,
    date_from: str | None = None,
    date_to: str | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """语义检索相关报告片段"""
    config = load_config()
    rag_config = config["rag"]
    collection = _get_collection(rag_config)
    embedding_model = rag_config["embedding_model"]

    if top_k is None:
        top_k = rag_config["top_k"]

    query_embedding = _embed([query], embedding_model)[0]

    where_filter = None
    if date_from or date_to:
        conditions = []
        if date_from:
            conditions.append({"date": {"$gte": date_from}})
        if date_to:
            conditions.append({"date": {"$lte": date_to}})
        where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where_filter,
        include=["documents", "metadatas", "distances"],
    )

    items = []
    max_chars_per_chunk = rag_config.get("max_chars_per_chunk", 300)
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        # 截断过长的片段，控制上下文总量
        truncated = doc[:max_chars_per_chunk] + ("..." if len(doc) > max_chars_per_chunk else "")
        items.append({
            "content": truncated,
            "full_content": doc,
            "date": meta["date"],
            "hour": meta["hour"],
            "section": meta["section"],
            "distance": dist,
        })

    return items


def index_all():
    """扫描 reports/ 目录，索引所有报告"""
    config = load_config()
    report_dir = SCRIPT_DIR / config["report"]["output_dir"]

    if not report_dir.exists():
        print("No reports directory found.")
        return

    count = 0
    for date_dir in sorted(report_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        date_str = date_dir.name
        for report_file in sorted(date_dir.glob("[0-9][0-9].md")):
            hour = int(report_file.stem)
            content = report_file.read_text()
            index_report(date_str, hour, content)
            count += 1

    print(f"\nTotal: indexed {count} reports")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--index-all", action="store_true", help="Index all existing reports")
    args = parser.parse_args()

    if args.index_all:
        index_all()
    else:
        print("Usage: python rag.py --index-all")
