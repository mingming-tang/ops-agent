"""语义向量层(供长期记忆的向量召回使用)。

OpenAI 兼容供应商(openai / qwen / minimax / deepseek)都提供 embedding 端点,
复用默认 ModelProvider 的 api_key / base_url 即可。Anthropic 无原生 embedding,
此时 get_embeddings() 返回 None,记忆召回自动降级为关键词匹配。

所有函数 best-effort:任何缺配置/网络/供应商异常都吞掉并返回 None,
绝不让记忆功能拖垮主流程。
"""
import math

from langchain_core.embeddings import Embeddings

from app.config import get_settings
from app.db.crypto import decrypt
from app.db.models import ModelProvider, ProviderType
from app.llm.registry import DEFAULT_BASE_URLS

settings = get_settings()

# 各 OpenAI 兼容供应商的默认 embedding 模型
_DEFAULT_EMBED_MODELS: dict[str, str] = {
    ProviderType.openai: "text-embedding-3-small",
    ProviderType.qwen: "text-embedding-v3",
    ProviderType.deepseek: "text-embedding-v3",   # deepseek 无自有 embedding,占位,失败则降级
    ProviderType.minimax: "embo-01",
}


def _default_provider() -> ModelProvider | None:
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        cfg = (db.query(ModelProvider).filter(ModelProvider.is_default).first()
               or db.query(ModelProvider).first())
        if cfg is not None:
            db.expunge(cfg)
        return cfg


def get_embeddings() -> Embeddings | None:
    """按默认供应商构造 embedding 客户端;不可用时返回 None。"""
    try:
        cfg = _default_provider()
        if cfg is None or cfg.provider_type == ProviderType.anthropic:
            return None  # Anthropic 无 embedding -> 降级关键词召回
        model = settings.embedding_model or _DEFAULT_EMBED_MODELS.get(cfg.provider_type)
        if not model:
            return None
        from langchain_openai import OpenAIEmbeddings

        base_url = cfg.base_url or DEFAULT_BASE_URLS.get(cfg.provider_type)
        return OpenAIEmbeddings(model=model, api_key=decrypt(cfg.api_key_enc), base_url=base_url)
    except Exception:  # noqa: BLE001
        return None


def embed_text(text: str) -> list[float] | None:
    """对单段文本取向量;失败返回 None。"""
    emb = get_embeddings()
    if emb is None or not text.strip():
        return None
    try:
        return emb.embed_query(text)
    except Exception:  # noqa: BLE001
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度(不引入 numpy 依赖)。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
