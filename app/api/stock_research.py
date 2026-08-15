"""股票研报智能体接口。

提供：
- POST /chat/stream — 流式研报对话（SSE）
- GET/POST/PUT/DELETE /prompts — 提示词模板 CRUD
- GET /reports — 历史研报列表（复用会话列表）

用户与会话归属同客服模块：登录用户用 user_id，访客用 guest_id。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.config import BASE_DIR as _BASE_DIR

from app.api._errors import wrap_llm_error
from app.api.auth import get_optional_user
from app.api.schemas import (
    SessionListResponse,
    SessionSummary,
    StockPromptCreateRequest,
    StockPromptItem,
    StockPromptUpdateRequest,
    StockResearchRequest,
)
from app.rag.chat_memory import get_chat_store, new_session_id
from app.rag.prompt_manager import (
    ensure_default_templates,
    create_template,
    delete_template,
    get_template,
    list_templates,
    render_template,
    update_template,
)
from app.rag.stock_tools import run_stock_agent_stream, _classify_query
from app.rag.user_store import User

router = APIRouter(prefix="/api/stock_research", tags=["stock_research"])

# 名称→代码解析规则（默认提示词与模板共用，避免模型凭记忆猜代码）
_NAME_RESOLUTION_RULE = (
    "如果用户输入的是股票名称/简称/拼音（如\"中国铝业\"、\"茅台\"、\"zgly\"）"
    "而非 6 位数字代码，必须**先调用 search_stock_code** 解析出正确的股票代码，"
    "再调用其他工具。严禁凭记忆猜测代码。"
)

# 默认研报提示词（未选择模板时使用，仅用于股票模式）
# 非股票问题会自动切换到万能助手模式（见 stock_tools._GENERAL_ASSISTANT_PROMPT）
_DEFAULT_PROMPT = """你是专业的股票分析师。请根据用户的问题，调用工具获取股票数据，生成专业的分析报告。

使用 Markdown 格式输出，包含以下模块（根据可用数据选择）：
## 行情摘要
## 技术面分析
## 基本面分析
## 资金面分析
## 新闻舆情
## 预测与建议

重要规则：
0. """ + _NAME_RESOLUTION_RULE + """
1. 每个工具只调用一次，同一个工具不要重复调用。
2. 如果工具返回错误（如"股票代码无效"、"数据获取失败"），不要重试，直接基于已有数据生成报告，并在报告中注明哪些数据不可用。
3. 最多调用 6 个工具，获取足够数据后立即生成报告。
4. 分析结论请用自然语言描述决策逻辑。
5. 引用数据时用 [来源1] [来源2] 等数字编号标注。"""


def _resolve_user_id(user: Optional[User], guest_id: Optional[str]) -> str:
    if user:
        return user.id
    if guest_id:
        return guest_id
    return ""


def _settings_ok() -> bool:
    from app.config import model_ready
    return model_ready()


def _extract_stock_identity(msg: str) -> tuple[str, str]:
    """从消息中提取股票代码/名称，返回 (code, name)。

    优先匹配 6 位数字代码；否则仅在消息为短关键词（名称/拼音）时尝试搜索。
    长句不猜，交给 agent 的 search_stock_code 工具解析，避免误判。
    """
    import re

    code = ""
    name = ""
    m = re.search(r"\b(\d{6})\b", msg)
    if m:
        code = m.group(1)
        return code, name

    keyword = msg.strip()
    if keyword and len(keyword) <= 12:
        try:
            from app.rag.stock_data import search_stock_code
            res = search_stock_code(keyword)
            matches = res.get("matches", []) if "error" not in res else []
            if matches:
                code = matches[0]["code"]
                name = matches[0]["name"]
        except Exception:
            pass
    return code, name


def _resolve_prompt(req: StockResearchRequest) -> str:
    """解析提示词：有 prompt_id 则渲染模板，否则用默认。

    注意：非股票问题会自动切换到万能助手模式（见 stock_tools._resolve_agent_params），
    这里的提示词仅用于股票模式。
    """
    if req.prompt_id:
        tpl = get_template(req.prompt_id)
        if not tpl:
            raise HTTPException(status_code=404, detail="提示词模板不存在")
        stock_code, stock_name = _extract_stock_identity(req.message)
        rendered = render_template(req.prompt_id, {
            "stock_code": stock_code,
            "stock_name": stock_name,
        }) or _DEFAULT_PROMPT
        # 模板本身未内置「名称→代码」规则，统一追加，避免模型凭记忆猜代码
        extra_rules: list[str] = []
        if not stock_code:
            extra_rules.append("0. " + _NAME_RESOLUTION_RULE)
        if extra_rules:
            rendered += "\n\n重要规则：\n" + "\n".join(extra_rules) + "\n"
        return rendered
    return _DEFAULT_PROMPT


@router.post("/chat/stream")
def chat_stream(
    req: StockResearchRequest,
    user: Optional[User] = Depends(get_optional_user),
):
    """流式研报对话（SSE）。

    事件流：
      {"type": "session", "data": {"session_id": ...}}
      {"type": "tool_call", "data": {...}}
      {"type": "token", "data": "片段"}
      {"type": "sources", "data": [...]}
      {"type": "done", "data": {...}}
      {"type": "error", "data": "msg"}
    """
    if not _settings_ok():
        raise HTTPException(status_code=500, detail="模型未就绪，请检查配置。")

    user_id = _resolve_user_id(user, getattr(req, "guest_id", None))
    session_id = req.session_id or new_session_id()

    if req.session_id:
        store = get_chat_store()
        owner = store.get_session_owner(req.session_id)
        if owner and owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")

    system_prompt = _resolve_prompt(req)
    detected_cls = _classify_query(req.message)
    detected_mode = "general" if detected_cls == "non_stock" else "stock"

    def event_gen():
        yield f"data: {json.dumps({'type': 'session', 'data': {'session_id': session_id, 'mode': detected_mode}}, ensure_ascii=False)}\n\n"
        for ev in run_stock_agent_stream(
            query=req.message,
            system_prompt=system_prompt,
            session_id=session_id,
            user_id=user_id,
        ):
            etype = ev.get("type")
            data = ev.get("data")
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


# —— 提示词模板 CRUD ——


@router.get("/prompts", response_model=list[StockPromptItem])
def list_prompts(category: Optional[str] = None) -> list[StockPromptItem]:
    """列出所有提示词模板。"""
    templates = list_templates(category)
    return [
        StockPromptItem(
            id=t.id, name=t.name, category=t.category, content=t.content,
            variables=t.variables, created_at=t.created_at, updated_at=t.updated_at,
        )
        for t in templates
    ]


@router.post("/prompts", response_model=StockPromptItem, status_code=201)
def create_prompt(req: StockPromptCreateRequest) -> StockPromptItem:
    """创建提示词模板。"""
    tpl = create_template(
        name=req.name, category=req.category, content=req.content,
    )
    return StockPromptItem(
        id=tpl.id, name=tpl.name, category=tpl.category, content=tpl.content,
        variables=tpl.variables, created_at=tpl.created_at, updated_at=tpl.updated_at,
    )


@router.put("/prompts/{template_id}", response_model=StockPromptItem)
def update_prompt(
    template_id: str, req: StockPromptUpdateRequest
) -> StockPromptItem:
    """更新提示词模板。"""
    tpl = update_template(
        template_id, name=req.name, category=req.category, content=req.content,
    )
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    return StockPromptItem(
        id=tpl.id, name=tpl.name, category=tpl.category, content=tpl.content,
        variables=tpl.variables, created_at=tpl.created_at, updated_at=tpl.updated_at,
    )


@router.delete("/prompts/{template_id}")
def delete_prompt(template_id: str) -> dict:
    """删除提示词模板。"""
    ok = delete_template(template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"ok": True, "id": template_id}


# —— 历史研报列表（复用会话列表） ——


@router.get("/reports", response_model=SessionListResponse)
def list_reports(
    user: Optional[User] = Depends(get_optional_user),
    guest_id: Optional[str] = None,
) -> SessionListResponse:
    """列出研报会话（复用会话列表接口）。"""
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


# —— PDF 研报：下载 + 基于 session 二次生成 ——

# 文件名安全白名单：只允许字母/数字/中文/_/./-，杜绝 ../../ 路径穿越
_SAFE_FILENAME_RE = re.compile(r"^[\w\u4e00-\u9fff\.\-\(\)]+$", flags=re.UNICODE)


def _pdf_dir() -> Path:
    d = _BASE_DIR / "data" / "pdfs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/download/{filename}")
def download_pdf(filename: str):
    """下载已生成的研报 PDF。

    路径：/api/stock_research/download/xxx.pdf
    做了严格的 filename 白名单校验，防止路径穿越。
    """
    if not _SAFE_FILENAME_RE.match(filename) or ".." in filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    fp = _pdf_dir() / filename
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="PDF 不存在或已过期")
    return FileResponse(
        path=str(fp),
        media_type="application/pdf",
        filename=filename,  # starlette 会自动按 RFC 5987 设置 Content-Disposition（含 filename*=UTF-8''...）
    )


class GeneratePdfRequest(BaseModel):
    session_id: str
    guest_id: Optional[str] = None


@router.post("/generate_pdf")
def generate_pdf_for_session(
    req: GeneratePdfRequest,
    user: Optional[User] = Depends(get_optional_user),
) -> dict:
    """基于已完成的会话重新生成 PDF（前端按钮「导出 PDF」直接调用）。

    从 chat_store 读取最近一轮的 assistant 回答作为正文；
    同时从 assistant 消息的 metadata 中提取 sources 和 tool_data，
    以便在 PDF 中呈现完整的参考来源和结构化数据板块。
    """
    if not req.session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")

    user_id = _resolve_user_id(user, req.guest_id)
    store = get_chat_store()
    owner = store.get_session_owner(req.session_id)
    if owner and user_id and owner != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    msgs = store.get_all(req.session_id)
    if not msgs:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 倒序找最近一条 assistant 消息作为正文
    last_assistant = ""
    last_user = ""
    last_stock_code = ""
    last_stock_name = ""
    last_sources: list[dict] = []
    last_tool_data: list[dict] = []
    for m in reversed(msgs):
        if m["role"] == "assistant" and not last_assistant:
            last_assistant = m.get("content", "") or ""
            meta = m.get("metadata") or {}
            last_sources = meta.get("sources", []) or []
            last_tool_data = meta.get("tool_data", []) or []
            last_stock_code = meta.get("stock_code", "") or ""
            last_stock_name = meta.get("stock_name", "") or ""
        if m["role"] == "user" and not last_user:
            last_user = m.get("content", "") or ""
        if last_assistant and last_user:
            break

    if not last_assistant.strip():
        raise HTTPException(status_code=400, detail="该会话尚无可用回答")

    # 如果 metadata 中没有股票信息，从 user 消息提取
    if not last_stock_code or not last_stock_name:
        code_from_user, name_from_user = _extract_stock_identity(last_user)
        if not last_stock_code:
            last_stock_code = code_from_user
        if not last_stock_name:
            last_stock_name = name_from_user

    # 兜底：从 assistant 内容提取 6 位代码
    if not last_stock_code:
        m = re.search(r"\b(\d{6})\b", last_assistant)
        if m:
            last_stock_code = m.group(1)

    try:
        from app.rag.pdf_report import generate_pdf_report

        result = generate_pdf_report(
            title="",
            stock_code=last_stock_code,
            stock_name=last_stock_name,
            content_md=last_assistant,
            sources=last_sources,
            tool_data=last_tool_data,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF 生成失败：{str(e)[:300]}")

    return {
        "ok": True,
        "file_name": result.file_name,
        "download_url": result.download_url,
        "file_size_kb": result.file_size_kb,
        "stock_code": last_stock_code,
        "stock_name": last_stock_name,
    }
