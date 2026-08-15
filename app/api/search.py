"""知识库检索问答接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api._errors import wrap_llm_error
from app.api.schemas import SearchRequest, SearchResponse, SourceDocument
from app.rag.chain import get_retrieval_chain
from app.rag.tools import run_agent
from app.rag.vectorstore import get_vectorstore

router = APIRouter(prefix="/api/search", tags=["search"])


def _docs_to_sources(docs) -> list[SourceDocument]:
    """把 LangChain Document 列表转为响应模型。"""
    sources: list[SourceDocument] = []
    for doc in docs or []:
        meta = doc.metadata or {}
        sources.append(
            SourceDocument(
                id=str(meta.get("id") or ""),
                title=meta.get("title") or "未命名",
                content=doc.page_content,
                source=meta.get("source") or None,
            )
        )
    return sources


@router.post("", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    """对知识库做 RAG 检索问答（固定链路：先检索再用检索结果生成答案）。"""
    if not settings_check():
        raise HTTPException(
            status_code=500,
            detail="未配置 DASHSCOPE_API_KEY，请在 .env 中设置后再检索。",
        )

    chain = get_retrieval_chain(k=req.k)
    try:
        result = chain.invoke({"query": req.query})
    except HTTPException:
        raise
    except Exception as e:
        raise wrap_llm_error(e)
    return SearchResponse(
        query=req.query,
        answer=result.get("answer", ""),
        sources=_docs_to_sources(result.get("source_documents")),
    )


@router.post("/agent", response_model=SearchResponse)
def search_agent(req: SearchRequest) -> SearchResponse:
    """Agent 模式：把向量检索封装为 tool 交给大模型，由模型自主决定是否检索。

    与固定链路 `/api/search` 的区别：
    - 模型可自行判断问题是否需要检索知识库（闲聊可不调用工具）。
    - 模型可自行改写检索关键词、多次检索。
    - 响应中 `tool_calls` 表示实际触发的检索次数。
    """
    if not settings_check():
        raise HTTPException(
            status_code=500,
            detail="未配置 DASHSCOPE_API_KEY，请在 .env 中设置后再检索。",
        )

    try:
        result = run_agent(query=req.query, k=req.k)
    except HTTPException:
        raise
    except Exception as e:
        raise wrap_llm_error(e)
    return SearchResponse(
        query=req.query,
        answer=result.get("answer", ""),
        sources=_docs_to_sources(result.get("source_documents")),
        tool_calls=result.get("tool_calls"),
    )


@router.post("/raw", response_model=list[SourceDocument])
def search_raw(req: SearchRequest) -> list[SourceDocument]:
    """仅做向量召回，不调用 LLM，便于调试。"""
    store = get_vectorstore()
    docs = store.similarity_search_with_score(req.query, k=req.k)
    items: list[SourceDocument] = []
    for doc, score in docs:
        meta = doc.metadata or {}
        items.append(
            SourceDocument(
                id=str(meta.get("id") or ""),
                title=meta.get("title") or "未命名",
                content=doc.page_content,
                source=meta.get("source") or None,
                score=float(score),
            )
        )
    return items


def settings_check() -> bool:
    from app.config import model_ready

    return model_ready()
