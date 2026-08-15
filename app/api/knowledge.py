"""知识库 CRUD 接口：向 Chroma 增删改查文档。"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from app.api.schemas import (
    BatchDeleteRequest,
    BatchDeleteResult,
    KnowledgeCreate,
    KnowledgeItem,
    KnowledgeUpdate,
    UploadResult,
)
from app.rag.loader import SUPPORTED_EXTS, load_and_split
from app.rag.vectorstore import get_vectorstore

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _doc_to_item(doc_id: str, metadata: dict, content: str) -> KnowledgeItem:
    """把 Chroma 内部文档转换为对外响应。"""
    return KnowledgeItem(
        id=doc_id,
        title=metadata.get("title", "未命名"),
        content=content,
        source=metadata.get("source"),
        created_at=datetime.fromisoformat(metadata["created_at"])
        if metadata.get("created_at")
        else datetime.now(),
    )


@router.post("", response_model=KnowledgeItem, status_code=status.HTTP_201_CREATED)
def create_knowledge(payload: KnowledgeCreate) -> KnowledgeItem:
    """新增一条知识到向量库。"""
    doc_id = str(uuid.uuid4())
    now_iso = datetime.now().isoformat()
    metadata = {
        "title": payload.title,
        "source": payload.source or "",
        "created_at": now_iso,
    }
    store = get_vectorstore()
    store.add_texts(
        texts=[payload.content],
        metadatas=[metadata],
        ids=[doc_id],
    )
    return _doc_to_item(doc_id, metadata, payload.content)


@router.post("/upload", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
async def upload_knowledge(file: UploadFile = File(...)) -> UploadResult:
    """上传文件，自动解析、切分后批量写入向量库。

    支持格式：txt / md / markdown / pdf。每个切分片段作为一条独立知识入库，
    metadata 中保留原文件名与 chunk 序号。
    """
    filename = file.filename or "unnamed.txt"
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}（仅支持 {', '.join(sorted(SUPPORTED_EXTS))}）",
        )

    content_bytes = await file.read()
    if not content_bytes:
        raise HTTPException(status_code=400, detail="上传文件为空")

    try:
        docs = load_and_split(filename, content_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    now_iso = datetime.now().isoformat()
    texts: list[str] = []
    metadatas: list[dict] = []
    ids: list[str] = []
    for doc in docs:
        doc_id = str(uuid.uuid4())
        meta = {
            "title": doc.metadata.get("title", Path(filename).stem),
            "source": doc.metadata.get("source", filename),
            "chunk_index": doc.metadata.get("chunk_index", 0),
            "created_at": now_iso,
        }
        texts.append(doc.page_content)
        metadatas.append(meta)
        ids.append(doc_id)

    store = get_vectorstore()
    store.add_texts(texts=texts, metadatas=metadatas, ids=ids)

    return UploadResult(
        filename=filename,
        title=Path(filename).stem,
        chunks=len(ids),
        ids=ids,
    )


@router.get("", response_model=list[KnowledgeItem])
def list_knowledge(
    keyword: Optional[str] = Query(None, description="按 title 模糊匹配"),
    limit: int = Query(100, ge=1, le=500),
) -> list[KnowledgeItem]:
    """列出所有知识条目（可选按标题关键字过滤）。"""
    store = get_vectorstore()
    collection = store._collection
    results = collection.get(limit=limit)  # type: ignore[arg-type]

    items: list[KnowledgeItem] = []
    ids = results.get("ids", []) or []
    docs = results.get("documents", []) or []
    metas = results.get("metadatas", []) or []

    for doc_id, content, meta in zip(ids, docs, metas):
        if not isinstance(meta, dict):
            continue
        title = meta.get("title", "")
        if keyword and keyword.lower() not in title.lower():
            continue
        items.append(_doc_to_item(doc_id, meta, content or ""))

    # 按 created_at 倒序，新的在前
    items.sort(key=lambda x: x.created_at, reverse=True)
    return items


@router.get("/{doc_id}", response_model=KnowledgeItem)
def get_knowledge(doc_id: str) -> KnowledgeItem:
    """根据 ID 获取单条知识。"""
    store = get_vectorstore()
    collection = store._collection
    result = collection.get(ids=[doc_id])
    ids = result.get("ids", []) or []
    if not ids:
        raise HTTPException(status_code=404, detail=f"知识不存在: {doc_id}")
    docs = result.get("documents", []) or []
    metas = result.get("metadatas", []) or []
    return _doc_to_item(ids[0], metas[0] or {}, docs[0] or "")


@router.put("/{doc_id}", response_model=KnowledgeItem)
def update_knowledge(doc_id: str, payload: KnowledgeUpdate) -> KnowledgeItem:
    """更新一条知识（Chroma 不支持原地更新文本，故采用删除后重建）。"""
    store = get_vectorstore()
    collection = store._collection

    existing = collection.get(ids=[doc_id])
    if not (existing.get("ids")):
        raise HTTPException(status_code=404, detail=f"知识不存在: {doc_id}")

    old_meta = (existing.get("metadatas") or [{}])[0] or {}
    old_content = (existing.get("documents") or [""])[0] or ""

    new_title = payload.title if payload.title is not None else old_meta.get("title", "未命名")
    new_content = payload.content if payload.content is not None else old_content
    new_source = payload.source if payload.source is not None else old_meta.get("source", "")
    now_iso = datetime.now().isoformat()

    # 先删后增
    collection.delete(ids=[doc_id])
    new_id = str(uuid.uuid4())
    metadata = {"title": new_title, "source": new_source, "created_at": now_iso}
    store.add_texts(texts=[new_content], metadatas=[metadata], ids=[new_id])
    return _doc_to_item(new_id, metadata, new_content)


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge(doc_id: str) -> None:
    """删除一条知识。"""
    store = get_vectorstore()
    collection = store._collection
    existing = collection.get(ids=[doc_id])
    if not existing.get("ids"):
        raise HTTPException(status_code=404, detail=f"知识不存在: {doc_id}")
    collection.delete(ids=[doc_id])


@router.post("/batch_delete", response_model=BatchDeleteResult)
def batch_delete_knowledge(payload: BatchDeleteRequest) -> BatchDeleteResult:
    """批量删除知识条目。

    对每个 ID 单独检查存在性，存在的删除、不存在的归入 not_found。
    任意一个 ID 删除失败不影响其他 ID。
    """
    store = get_vectorstore()
    collection = store._collection

    # 一次性查询所有 ID 的存在性
    existing = collection.get(ids=list(payload.ids))
    existing_ids = set(existing.get("ids") or [])

    deleted: list[str] = []
    not_found: list[str] = []
    to_delete: list[str] = []
    for doc_id in payload.ids:
        if doc_id in existing_ids:
            to_delete.append(doc_id)
        else:
            not_found.append(doc_id)

    if to_delete:
        collection.delete(ids=to_delete)

    deleted = to_delete
    return BatchDeleteResult(
        deleted=deleted,
        not_found=not_found,
        deleted_count=len(deleted),
    )
