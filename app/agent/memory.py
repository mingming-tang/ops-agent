"""长期记忆存取层:写入、向量/关键词召回、自动抽取。

- 写入:add_memory(content) —— 生成 embedding(可用时)后入库,按正文精确去重。
- 召回:search_memories(query, k) —— 有向量则余弦排序取 top-k,否则关键词 + 最近优先兜底。
- 抽取:extract_and_store(messages, model) —— 让模型从一段对话里提炼"值得长期记住的事实",逐条入库。

所有抽取/向量相关路径 best-effort,异常不外抛,避免拖垮对话主流程。
"""
import json
import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.prompts import MEMORY_EXTRACT_PROMPT
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import Memory
from app.llm.embeddings import cosine, embed_text

settings = get_settings()

_MAX_VECTOR_SCAN = 2000   # 向量召回时最多扫描的记忆条数(按时间倒序取)


def add_memory(content: str, kind: str = "fact", source_thread_id: str | None = None) -> int | None:
    """写入一条长期记忆;正文已存在则跳过。返回记忆 id(或已存在条目的 id)。"""
    content = (content or "").strip()
    if not content:
        return None
    with SessionLocal() as db:
        existing = db.query(Memory).filter(Memory.content == content).first()
        if existing is not None:
            return existing.id
        vec = embed_text(content)
        m = Memory(content=content, kind=(kind or "fact"),
                   source_thread_id=source_thread_id, embedding=vec)
        db.add(m)
        db.commit()
        return m.id


def search_memories(query: str, k: int = 5) -> list[dict]:
    """召回与 query 最相关的记忆。返回 [{id, content, kind, score}]。"""
    query = (query or "").strip()
    if not query or k <= 0:
        return []
    qvec = embed_text(query)
    with SessionLocal() as db:
        if qvec is not None:
            rows = (db.query(Memory).order_by(Memory.created_at.desc())
                    .limit(_MAX_VECTOR_SCAN).all())
            scored = [(cosine(qvec, m.embedding), m) for m in rows if m.embedding]
            # 没有任何向量化记忆时,退回关键词召回
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                return [{"id": m.id, "content": m.content, "kind": m.kind, "score": round(s, 4)}
                        for s, m in scored[:k] if s > 0]
        # 关键词 + 最近优先兜底
        terms = [t for t in re.split(r"\s+", query) if t]
        q = db.query(Memory)
        if terms:
            from sqlalchemy import or_
            q = q.filter(or_(*[Memory.content.like(f"%{t}%") for t in terms]))
        rows = q.order_by(Memory.created_at.desc()).limit(k).all()
        return [{"id": m.id, "content": m.content, "kind": m.kind, "score": None} for m in rows]


def recent_memories(limit: int = 100) -> list[dict]:
    with SessionLocal() as db:
        rows = db.query(Memory).order_by(Memory.created_at.desc()).limit(limit).all()
        return [{"id": m.id, "content": m.content, "kind": m.kind,
                 "source_thread_id": m.source_thread_id,
                 "created_at": m.created_at.isoformat()} for m in rows]


def format_memories(items: list[dict]) -> str:
    """把召回的记忆拼成可读文本(注入提示词 / 回灌工具结果)。"""
    if not items:
        return ""
    return "\n".join(f"- [{it['kind']}] {it['content']}" for it in items)


def _render_for_extract(messages: list) -> str:
    """把一段 LangChain 消息序列化成纯文本,供抽取用。"""
    lines: list[str] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        content = m.content if isinstance(m.content, str) else str(m.content)
        if isinstance(m, HumanMessage):
            lines.append(f"用户:{content}")
        elif isinstance(m, AIMessage):
            if content.strip():
                lines.append(f"助手:{content}")
            for tc in (getattr(m, "tool_calls", None) or []):
                lines.append(f"助手调用 {tc['name']}:{json.dumps(tc['args'], ensure_ascii=False)}")
        elif isinstance(m, ToolMessage):
            lines.append(f"工具结果({getattr(m, 'name', '')}):{content[:1000]}")
    return "\n".join(lines)


async def extract_and_store(messages: list, model: BaseChatModel,
                            thread_id: str | None = None) -> int:
    """从一段对话中抽取值得长期记住的事实并入库。返回新增条数。best-effort。"""
    if not settings.memory_auto_extract or not messages:
        return 0
    transcript = _render_for_extract(messages)
    if not transcript.strip():
        return 0
    try:
        resp = await model.ainvoke([
            SystemMessage(content=MEMORY_EXTRACT_PROMPT),
            HumanMessage(content=transcript[:12000]),
        ])
        facts = _parse_facts(resp.content if isinstance(resp.content, str) else str(resp.content))
    except Exception:  # noqa: BLE001
        return 0
    added = 0
    for f in facts:
        content = (f.get("content") or "").strip()
        if content and add_memory(content, f.get("kind", "fact"), thread_id) is not None:
            added += 1
    return added


def _parse_facts(text: str) -> list[dict]:
    """从模型输出里抽出 JSON 数组(容忍 ```json 包裹与前后噪声)。"""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("content")]
