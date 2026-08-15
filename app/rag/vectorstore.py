"""Chroma 向量库封装，提供单例 VectorStore。"""

from __future__ import annotations

from typing import Optional

from langchain_chroma import Chroma

from app.config import settings
from app.rag.embeddings import get_embeddings

_vectorstore: Optional[Chroma] = None


def get_vectorstore() -> Chroma:
    """返回持久化的 Chroma 向量库单例。"""
    global _vectorstore
    if _vectorstore is None:
        _vectorstore = Chroma(
            collection_name=settings.chroma_collection_name,
            embedding_function=get_embeddings(),
            persist_directory=str(settings.chroma_persist_dir),
        )
    return _vectorstore
