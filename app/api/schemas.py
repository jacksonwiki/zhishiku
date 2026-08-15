"""请求 / 响应数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class KnowledgeCreate(BaseModel):
    """新增知识条目。"""

    title: str = Field(..., min_length=1, max_length=200, description="标题")
    content: str = Field(..., min_length=1, description="正文内容")
    source: Optional[str] = Field(None, max_length=200, description="来源标识（可选）")


class KnowledgeUpdate(BaseModel):
    """更新知识条目（字段均可选）。"""

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    content: Optional[str] = Field(None, min_length=1)
    source: Optional[str] = Field(None, max_length=200)


class KnowledgeItem(BaseModel):
    """知识条目返回结构。"""

    id: str
    title: str
    content: str
    source: Optional[str] = None
    created_at: datetime


class UploadResult(BaseModel):
    """文件上传结果。"""

    filename: str
    title: str
    chunks: int = Field(..., description="切分后写入向量库的片段数量")
    ids: list[str]


class BatchDeleteRequest(BaseModel):
    """批量删除请求。"""

    ids: list[str] = Field(..., min_length=1, description="待删除的知识 ID 列表")


class BatchDeleteResult(BaseModel):
    """批量删除结果。"""

    deleted: list[str] = Field(..., description="成功删除的 ID 列表")
    not_found: list[str] = Field(..., description="未找到的 ID 列表")
    deleted_count: int


class SearchRequest(BaseModel):
    """检索问答请求。"""

    query: str = Field(..., min_length=1, description="用户问题")
    k: int = Field(4, ge=1, le=10, description="召回文档数量")


class SourceDocument(BaseModel):
    """命中的来源文档。"""

    id: str
    title: str
    content: str
    source: Optional[str] = None
    score: Optional[float] = None


class SearchResponse(BaseModel):
    """检索问答响应。"""

    query: str
    answer: str
    sources: list[SourceDocument]
    tool_calls: Optional[int] = Field(None, description="Agent 模式下工具调用次数；普通模式为 None")


class ChatMessage(BaseModel):
    """单条对话消息（用于智能客服多轮历史）。"""

    role: str = Field(..., pattern="^(user|assistant)$", description="消息角色")
    content: str = Field(..., min_length=1, description="消息内容")


class CustomerServiceRequest(BaseModel):
    """智能客服请求。"""

    message: str = Field(..., min_length=1, description="本轮用户问题")
    history: list[ChatMessage] = Field(default_factory=list, description="历史对话（无 session_id 时使用）")
    k: int = Field(4, ge=1, le=10, description="检索召回数量")
    session_id: Optional[str] = Field(
        None,
        description="会话 id；传入则启用 SQLite 持久化记忆与压缩，不传则用 history",
    )
    guest_id: Optional[str] = Field(
        None,
        description="访客 id；未登录时用此标识会话归属（登录用户忽略此字段）",
    )


class ToolCallInfo(BaseModel):
    """一次工具调用信息（展示 Agent 思考过程）。"""

    name: str
    args: dict
    result_preview: str
    sources_count: int


class CustomerServiceResponse(BaseModel):
    """智能客服响应。"""

    answer: str
    tool_calls: list[ToolCallInfo] = Field(default_factory=list, description="本轮 agent 调用的工具列表")
    sources: list[SourceDocument] = Field(default_factory=list, description="命中的来源文档")
    session_id: Optional[str] = Field(None, description="会话 id（用于后续请求回传）")


# —— 会话历史与列表 ——

class SessionSummary(BaseModel):
    """会话列表项。"""

    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    """用户会话列表响应。"""

    sessions: list[SessionSummary]
    total: int


class HistoryMessage(BaseModel):
    """历史消息项。"""

    role: str
    content: str
    turn_index: int
    metadata: Optional[dict] = None


class SessionHistoryResponse(BaseModel):
    """单会话历史响应。"""

    session_id: str
    title: str
    messages: list[HistoryMessage]


class SessionTitleUpdateRequest(BaseModel):
    """会话标题更新请求。"""

    title: str = Field(..., min_length=1, max_length=100, description="新标题")


# —— 股票研报 ——


class StockResearchRequest(BaseModel):
    """股票研报对话请求。"""

    message: str = Field(..., min_length=1, description="用户问题（含股票代码或名称）")
    prompt_id: Optional[str] = Field(None, description="提示词模板 id；不传则用默认深度研报模板")
    session_id: Optional[str] = Field(None, description="会话 id")
    guest_id: Optional[str] = Field(None, description="访客 id")


class StockPromptCreateRequest(BaseModel):
    """创建提示词模板请求。"""

    name: str = Field(..., min_length=1, max_length=100, description="模板名称")
    category: str = Field(..., pattern="^(quick|deep|industry|custom)$", description="分类")
    content: str = Field(..., min_length=1, description="模板内容，支持 {{stock_code}} {{stock_name}} 变量")


class StockPromptUpdateRequest(BaseModel):
    """更新提示词模板请求。"""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    category: Optional[str] = Field(None, pattern="^(quick|deep|industry|custom)$")
    content: Optional[str] = Field(None, min_length=1)


class StockPromptItem(BaseModel):
    """提示词模板返回结构。"""

    id: str
    name: str
    category: str
    content: str
    variables: list[str]
    created_at: int
    updated_at: int
