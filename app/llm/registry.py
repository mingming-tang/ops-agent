"""统一模型层。

把 5 家供应商收敛成一个接口。Qwen / MiniMax / DeepSeek 都提供 OpenAI 兼容端点,
因此除 Anthropic 外全部复用 langchain_openai.ChatOpenAI,只换 base_url。
返回的对象都支持 `.bind_tools()`,可直接喂给 LangGraph。
"""
from langchain_core.language_models import BaseChatModel

from app.db.crypto import decrypt
from app.db.models import ModelProvider, ProviderType

# 各供应商 OpenAI 兼容端点默认值(用户可在后台覆盖 base_url)
DEFAULT_BASE_URLS: dict[str, str] = {
    ProviderType.qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ProviderType.minimax: "https://api.minimaxi.com/v1",
    ProviderType.deepseek: "https://api.deepseek.com/v1",
}


def build_chat_model(cfg: ModelProvider) -> BaseChatModel:
    api_key = decrypt(cfg.api_key_enc)
    extra = cfg.extra or {}

    if cfg.provider_type == ProviderType.anthropic:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=cfg.model_name,
            api_key=api_key,
            base_url=cfg.base_url or None,
            temperature=cfg.temperature,
            stream_usage=True,          # 流式时也返回 token 用量
            **extra,
        )

    # openai / qwen / minimax / deepseek —— 全部走 OpenAI 兼容协议
    from langchain_openai import ChatOpenAI

    base_url = cfg.base_url or DEFAULT_BASE_URLS.get(cfg.provider_type)
    return ChatOpenAI(
        model=cfg.model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=cfg.temperature,
        stream_usage=True,              # 流式时也返回 token 用量(OpenAI 兼容端点)
        **extra,
    )
