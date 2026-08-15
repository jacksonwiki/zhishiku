"""大语言模型封装。

根据 settings.llm_provider 选择：
- dashscope：阿里云千问（ChatTongyi）
- ollama：本地 Ollama（ChatOllama）
- deepseek：深度求索（ChatDeepSeek）
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import settings


def get_llm(max_tokens: int | None = None) -> BaseChatModel:
    """返回对话模型实例。

    Args:
        max_tokens: 可选，限制单次生成的最大 token 数。
            ollama 映射为 num_predict，dashscope/deepseek 映射为 max_tokens。
            用于历史摘要等需要严格控制输出长度的场景。
    """
    provider = settings.llm_provider.lower()
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs: dict = {
            "model": settings.llm_model,
            "base_url": settings.ollama_base_url,
            "temperature": 0.3,
            # 关闭 qwen3 思考模式：reasoning=False 映射为 ollama API 的 think=false，
            # 直接输出答案，避免 token 浪费在推理上，加快响应。
            "reasoning": False,
        }
        if max_tokens is not None:
            kwargs["num_predict"] = max_tokens
        return ChatOllama(**kwargs)

    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        kwargs = {
            "model": settings.llm_model,
            "api_key": settings.deepseek_api_key,
            "temperature": 0.3,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return ChatDeepSeek(**kwargs)

    # 默认 dashscope
    from langchain_community.chat_models import ChatTongyi

    kwargs = {
        "model": settings.llm_model,
        "dashscope_api_key": settings.dashscope_api_key,
        "streaming": False,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatTongyi(**kwargs)