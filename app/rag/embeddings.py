"""向量模型封装。

根据 settings.embedding_provider 选择：
- dashscope：阿里云千问（DashScopeEmbeddings）
- ollama：本地 Ollama（OllamaEmbeddings）
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings

from app.config import settings


def get_embeddings() -> Embeddings:
    """返回 Embedding 模型实例。"""
    provider = settings.embedding_provider.lower()
    if provider == "ollama":
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(
            model=settings.embedding_model,
            base_url=settings.ollama_base_url,
        )
    # 默认 dashscope
    from langchain_community.embeddings import DashScopeEmbeddings

    return DashScopeEmbeddings(
        model=settings.embedding_model,
        dashscope_api_key=settings.dashscope_api_key,
    )
