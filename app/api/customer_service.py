"""智能客服接口（基于 tool-calling agent）。

agent 自主决定是否调用工具（知识库检索等），支持多轮对话。
新增工具只需在 app/rag/tools.py 的 get_tool_specs() 中追加。

用户与会话归属：
- 登录用户用 user_id 绑定会话；访客用 guest id（前端生成或调 /api/auth/guest）。
- 会话列表/历史/删除接口要求 user_id 与会话归属一致才可操作。
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.documents import Document
from pydantic import BaseModel

from app.api._errors import wrap_llm_error
from app.api.auth import get_optional_user
from app.api.schemas import (
    CustomerServiceRequest,
    CustomerServiceResponse,
    SessionHistoryResponse,
    SessionListResponse,
    SessionSummary,
    SessionTitleUpdateRequest,
    SourceDocument,
    ToolCallInfo,
)
from app.rag.chat_memory import get_chat_store, new_session_id
from app.rag.tools import run_agent, run_agent_stream
from app.rag.user_store import User

router = APIRouter(prefix="/api/customer_service", tags=["customer_service"])

def _build_customer_service_prompt() -> str:
    """根据是否启用 Tavily 动态构建客服提示词。"""
    from app.rag.tavily_search import is_enabled as tavily_enabled

    tools_desc = "- search_knowledge_base：检索本地知识库，获取产品规则、制度、文档等事实性信息。"
    fallback_rule = '3. 严格依据检索到的资料作答；资料不足以回答时如实告知"根据当前知识库，我暂时无法回答该问题，建议您联系人工客服"。'

    if tavily_enabled():
        tools_desc += (
            "\n- tavily_web_search：调用 Tavily 进行网络搜索，获取最新或本地知识库未覆盖的信息。"
        )
        fallback_rule = (
            "3. 严格依据检索/搜索到的资料作答；资料不足以回答时如实告知。\n"
            "4. **工具调用顺序**：先调用 search_knowledge_base 检索本地知识库；"
            "若返回\"未检索到相关资料\"且问题需要外部信息（最新动态、新闻、外部产品文档等），"
            "再调用 tavily_web_search 搜索网络。不要用网络搜索回答知识库内已有的事实性问题。"
        )

    return f"""你是一个友好、专业的智能客服助手。

你可以使用工具来更好地回答用户问题：
{tools_desc}

回答原则：
1. 涉及知识库中的事实、制度、产品规则、业务条款等问题，先调用 search_knowledge_base 检索，再依据检索结果作答，不要凭记忆编造。
2. 打招呼、闲聊、通用常识问题可直接回答，无需调用工具。
{fallback_rule}
{"5. 风格友好、简洁、专业，使用中文，适当使用分点。" if tavily_enabled() else "4. 风格友好、简洁、专业，使用中文，适当使用分点。"}
{"6. 引用资料时用 [来源1] [来源2] 等数字编号标注在对应内容后面，不要使用 [来源N] [来源M] 等字母。" if tavily_enabled() else "5. 引用资料时用 [来源1] [来源2] 等数字编号标注在对应内容后面，不要使用 [来源N] [来源M] 等字母。"}
{"7. 关注多轮对话上下文，不重复询问已确认的信息。" if tavily_enabled() else "6. 关注多轮对话上下文，不重复询问已确认的信息。"}
"""


_CUSTOMER_SERVICE_PROMPT = _build_customer_service_prompt()


def _settings_ok() -> bool:
    from app.config import model_ready

    return model_ready()


def _docs_to_sources(docs) -> list[SourceDocument]:
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


def _resolve_user_id(user: Optional[User], guest_id: Optional[str]) -> str:
    """解析出 user_id：登录用户优先；否则用 guest_id；都没有则生成临时访客。"""
    if user:
        return user.id
    if guest_id:
        return guest_id
    return ""


@router.post("/chat", response_model=CustomerServiceResponse)
def chat(
    req: CustomerServiceRequest,
    user: Optional[User] = Depends(get_optional_user),
) -> CustomerServiceResponse:
    """智能客服对话：由 agent 自主决定是否调用工具。

    - 已登录用户：会话归属到该用户 id。
    - 访客：用 guest_id 字段标识；首次无 session_id 则自动生成。
    """
    if not _settings_ok():
        raise HTTPException(
            status_code=500,
            detail="模型未就绪，请检查配置。",
        )

    user_id = _resolve_user_id(user, getattr(req, "guest_id", None))
    # 没传 session_id 则自动生成，启用 SQLite 持久化记忆
    session_id = req.session_id or new_session_id()

    # 会话归属校验：若会话已存在，必须归属当前 user_id
    if req.session_id:
        store = get_chat_store()
        owner = store.get_session_owner(req.session_id)
        if owner and owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")

    history = [{"role": h.role, "content": h.content} for h in req.history]
    try:
        result = run_agent(
            query=req.message,
            history=history,
            k=req.k,
            system_prompt=_CUSTOMER_SERVICE_PROMPT,
            session_id=session_id,
            user_id=user_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise wrap_llm_error(e)

    tool_calls = [ToolCallInfo(**tc) for tc in result.get("tool_call_records", [])]
    return CustomerServiceResponse(
        answer=result.get("answer", ""),
        tool_calls=tool_calls,
        sources=_docs_to_sources(result.get("source_documents")),
        session_id=session_id,
    )


def _doc_to_source_dict(doc: Document) -> dict:
    """把 Document 转成可序列化的 dict（用于 SSE）。"""
    meta = doc.metadata or {}
    return {
        "id": str(meta.get("id") or ""),
        "title": meta.get("title") or "未命名",
        "content": doc.page_content,
        "source": meta.get("source") or None,
    }


@router.post("/chat/stream")
def chat_stream(
    req: CustomerServiceRequest,
    user: Optional[User] = Depends(get_optional_user),
):
    """智能客服流式对话（SSE）。

    事件流（每行 `data: {json}\\n\\n`）：
      {"type": "session", "data": {"session_id": ...}}   首事件，回传 session_id
      {"type": "tool_call", "data": {...}}               工具调用详情
      {"type": "token", "data": "片段"}                  答案增量
      {"type": "sources", "data": [...]}                 命中来源文档列表
      {"type": "done", "data": {"tool_calls": N}}
      {"type": "error", "data": "msg"}
    """
    if not _settings_ok():
        raise HTTPException(
            status_code=500,
            detail="模型未就绪，请检查配置。",
        )

    user_id = _resolve_user_id(user, getattr(req, "guest_id", None))
    session_id = req.session_id or new_session_id()

    if req.session_id:
        store = get_chat_store()
        owner = store.get_session_owner(req.session_id)
        if owner and owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")

    history = [{"role": h.role, "content": h.content} for h in req.history]

    def event_gen():
        # 首个事件返回 session_id，便于前端保存并在后续请求回传
        yield f"data: {json.dumps({'type': 'session', 'data': {'session_id': session_id}}, ensure_ascii=False)}\n\n"
        for ev in run_agent_stream(
            query=req.message,
            history=history,
            k=req.k,
            system_prompt=_CUSTOMER_SERVICE_PROMPT,
            session_id=session_id,
            user_id=user_id,
        ):
            etype = ev.get("type")
            data = ev.get("data")
            if etype == "sources":
                # Document 不可直接 json 序列化，转 dict
                data = [_doc_to_source_dict(d) for d in data]
            yield f"data: {json.dumps({'type': etype, 'data': data}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# —— 会话历史与管理 ——


class GuestIdRequest(BaseModel):
    """带 guest_id 的请求体（用于会话管理接口）。"""

    guest_id: Optional[str] = None


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    user: Optional[User] = Depends(get_optional_user),
    guest_id: Optional[str] = None,
) -> SessionListResponse:
    """列出当前用户（或访客）的会话列表。"""
    user_id = _resolve_user_id(user, guest_id)
    if not user_id:
        return SessionListResponse(sessions=[], total=0)
    store = get_chat_store()
    rows = store.list_sessions(user_id)
    sessions = [
        SessionSummary(
            session_id=r["session_id"],
            title=r["title"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    return SessionListResponse(sessions=sessions, total=len(sessions))


def _require_session_owner(session_id: str, user_id: str) -> None:
    """校验会话存在且归属 user_id；否则抛 403/404。"""
    store = get_chat_store()
    owner = store.get_session_owner(session_id)
    if not owner:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")


@router.get("/sessions/{session_id}/history", response_model=SessionHistoryResponse)
def get_session_history(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
    guest_id: Optional[str] = None,
) -> SessionHistoryResponse:
    """获取指定会话的完整历史消息。"""
    user_id = _resolve_user_id(user, guest_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="未识别用户身份")
    _require_session_owner(session_id, user_id)
    store = get_chat_store()
    msgs = store.get_all(session_id)
    # 会话标题
    sessions = store.list_sessions(user_id)
    title = next((s["title"] for s in sessions if s["session_id"] == session_id), "")
    return SessionHistoryResponse(
        session_id=session_id,
        title=title,
        messages=msgs,
    )


@router.put("/sessions/{session_id}/title")
def rename_session(
    session_id: str,
    req: SessionTitleUpdateRequest,
    user: Optional[User] = Depends(get_optional_user),
    guest_id: Optional[str] = None,
) -> dict:
    """重命名会话标题。"""
    user_id = _resolve_user_id(user, guest_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="未识别用户身份")
    _require_session_owner(session_id, user_id)
    store = get_chat_store()
    ok = store.rename_session(session_id, req.title)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "title": req.title}


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
    guest_id: Optional[str] = None,
) -> dict:
    """删除指定会话及其全部消息与摘要。"""
    user_id = _resolve_user_id(user, guest_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="未识别用户身份")
    _require_session_owner(session_id, user_id)
    store = get_chat_store()
    store.clear_session(session_id)
    return {"ok": True, "session_id": session_id}
