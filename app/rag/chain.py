"""RAG 检索问答链（基于 LCEL）。"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda

from app.rag.llm import get_llm
from app.rag.vectorstore import get_vectorstore

_SYSTEM_PROMPT = """你是一个严谨的中文知识库问答助手。请严格依据下方"参考资料"回答用户问题。
- 如果参考资料中包含答案，请直接、完整地作答，并在答案末尾以 [来源N] 形式标注引用的资料编号。
- 如果参考资料不足以回答问题，请回复："根据当前知识库，我无法回答该问题。"，不要编造内容。

参考资料：
{context}
"""


def _format_docs(docs: list[Document]) -> str:
    """把召回的文档拼接成上下文，每条带 [来源N] 标号。"""
    if not docs:
        return "（暂无相关资料）"
    blocks = []
    for i, doc in enumerate(docs, start=1):
        content = doc.page_content.strip()
        source = doc.metadata.get("title") or doc.metadata.get("source") or "未命名"
        blocks.append(f"[来源{i}] ({source})\n{content}")
    return "\n\n".join(blocks)


def get_retrieval_chain(k: int = 4) -> Runnable:
    """构建 RAG 检索-生成链。

    Args:
        k: 检索时召回的文档数量。

    Returns:
        Runnable，输入 {"query": "..."}，输出 dict：
          {"answer": str, "source_documents": list[Document]}
    """
    vectorstore = get_vectorstore()
    retriever = vectorstore.as_retriever(search_kwargs={"k": k})

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _SYSTEM_PROMPT),
            ("human", "{query}"),
        ]
    )

    llm = get_llm()
    answer_chain = prompt | llm | StrOutputParser()

    def _retrieve(payload: dict[str, Any]) -> dict[str, Any]:
        query = payload["query"]
        docs = retriever.invoke(query)
        return {"query": query, "context": _format_docs(docs), "source_documents": docs}

    return (
        RunnableLambda(_retrieve)
        | {
            "answer": answer_chain,
            "source_documents": lambda x: x["source_documents"],
        }
    )
