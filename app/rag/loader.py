"""上传文件的加载与切分。

支持格式：
    - .txt / .md / .markdown：纯文本直接读取
    - .pdf：使用 pypdf 解析

输出：list[Document]，已按 RecursiveCharacterTextSplitter 切分为 chunk。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 支持的扩展名（小写，含点）
SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".pdf"}

# 默认切分参数
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80


def get_splitter(chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP) -> RecursiveCharacterTextSplitter:
    """构造文本切分器。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
    )


def _load_raw(filename: str, content_bytes: bytes) -> str:
    """根据扩展名把文件字节解析为纯文本。"""
    ext = Path(filename).suffix.lower()
    if ext in {".txt", ".md", ".markdown"}:
        # 优先 utf-8，回退 gbk（兼容 Windows 中文 txt）
        for enc in ("utf-8", "utf-8-sig", "gbk"):
            try:
                return content_bytes.decode(enc)
            except UnicodeDecodeError:
                continue
        return content_bytes.decode("utf-8", errors="ignore")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("解析 PDF 需要安装 pypdf：pip install pypdf") from e
        from io import BytesIO

        reader = PdfReader(BytesIO(content_bytes))
        parts = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
        return "\n\n".join(parts).strip()
    raise ValueError(f"不支持的文件类型: {ext}（仅支持 {', '.join(SUPPORTED_EXTS)}）")


def load_and_split(
    filename: str,
    content_bytes: bytes,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[Document]:
    """解析上传文件并切分为 Document 列表。

    每个 chunk 的 metadata 仅包含与原文件相关的信息（title/source），
    入库时再补充 id/created_at/chunk_index 等。
    """
    text = _load_raw(filename, content_bytes)
    if not text.strip():
        raise ValueError("文件内容为空，无法解析")

    stem = Path(filename).stem
    splitter = get_splitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = splitter.split_text(text)

    docs: list[Document] = []
    for idx, chunk in enumerate(chunks):
        docs.append(
            Document(
                page_content=chunk,
                metadata={
                    "title": stem,
                    "source": filename,
                    "chunk_index": idx,
                },
            )
        )
    return docs
