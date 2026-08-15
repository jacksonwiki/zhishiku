"""把能力封装为 LangChain Tool，交给大模型自主调用（Agentic RAG / 智能客服）。

设计要点：
1. 使用 langchain.agents.create_agent 构建 agent 图，自动管理 tool-calling 循环。
2. 每个工具用 @tool 装饰器定义为标准 BaseTool；新增工具只需在 _build_tools_with_context 追加。
3. 工具执行时通过闭包 _RunContext 收集命中文档和调用记录，供前端展示 Agent 思考过程。
4. run_agent / run_agent_stream 保留原对外接口，内部改为 create_agent 实现，支持流式 token。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from langchain.agents import create_agent
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import BaseTool, tool

from app.rag.chat_memory import ChatMemoryStore, get_chat_store
from app.rag.llm import get_llm
from app.rag.tavily_search import is_enabled as tavily_enabled
from app.rag.tavily_search import format_results as tavily_format
from app.rag.tavily_search import search as tavily_search
from app.rag.vectorstore import get_vectorstore


# —— 对话历史压缩配置（滑动窗口 + Token 阈值 + 前置摘要）——
# 1 轮 = 1 user + 1 assistant = 2 条消息
_RECENT_ROUNDS = 4            # 滑动窗口：保留最近 4 轮（8 条）原文
_MAX_TOKENS = 2048            # 历史总 token 阈值；超过则触发摘要压缩
_SUMMARY_MAX_TOKENS = 256     # 摘要生成时的 max_tokens 限制

# 摘要提示词：保留用户核心需求 / 身份信息 / 客服关键结论
_SUMMARY_PROMPT = (
    "请将以下对话历史压缩成一段简洁的中文摘要（不超过 150 字），"
    "保留用户的核心需求、身份信息以及客服给出的关键结论与依据。"
    "省略寒暄和无关细节。只输出摘要正文，不要复述原文。\n\n"
    "待压缩的对话历史：\n{messages}"
)


@lru_cache(maxsize=1)
def _chinese_pattern() -> re.Pattern:
    """中文及中文标点字符集（用于 token 估算）。"""
    return re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def _count_tokens(text: str) -> int:
    """粗略估算 token 数：中文按 1 字符 / token，非中文按 4 字符 / token。

    ollama qwen 系列无标准 tokenizer，此估算在长文本场景下足够稳定。
    """
    if not text:
        return 0
    zh = len(_chinese_pattern().findall(text))
    other = len(text) - zh
    return zh + (other + 3) // 4


def _messages_to_text(msgs: list[dict]) -> str:
    """消息列表转文本，用于 token 统计与摘要输入。"""
    return "\n".join(f"[{m.get('role', 'user')}] {m.get('content', '')}" for m in msgs)


def _summarize_with_llm(msgs: list[dict], existing_summary: str = "") -> str:
    """调用小模型把消息列表压缩为摘要文本。

    若已有旧摘要，则做增量融合（避免每次重压全部历史）。
    失败时返回旧摘要（保证服务可用）。
    """
    if not msgs:
        return existing_summary
    prompt = _SUMMARY_PROMPT.format(messages=_messages_to_text(msgs))
    if existing_summary:
        prompt = (
            f"已有摘要：\n{existing_summary}\n\n"
            f"请在此基础上融合以下新对话内容，更新摘要：\n{prompt}"
        )
    llm = get_llm(max_tokens=_SUMMARY_MAX_TOKENS)
    try:
        resp = llm.invoke(prompt)
        content = getattr(resp, "content", "") or ""
        content = content.strip()
        return content or existing_summary
    except Exception:
        return existing_summary


def _compress_history(
    history: list[dict],
    store: ChatMemoryStore | None = None,
    session_id: str | None = None,
) -> tuple[str, list[dict]]:
    """滑动窗口 + Token 阈值 + 前置摘要。

    Args:
        history: 全部历史消息，每项含 role/content[/turn_index]。
        store: 持久化存储；传入则复用已存摘要并增量更新。
        session_id: 会话 id；传入则读写持久化摘要。

    Returns:
        (summary, recent_msgs):
          - summary: 前置摘要文本（可能为空，表示未触发压缩）
          - recent_msgs: 窗口内的原文消息（最长 _RECENT_ROUNDS*2 条）
    """
    if not history:
        return "", []

    window_size = _RECENT_ROUNDS * 2
    if len(history) <= window_size:
        # 还没攒够窗口，无需压缩；但若有已存摘要则带上
        existing = ""
        if store is not None and session_id:
            existing, _ = store.get_summary(session_id)
        return existing, history

    recent_msgs = list(history[-window_size:])
    older_msgs = list(history[:-window_size])

    # Token 阈值判断：未超阈值则不压缩（带已存摘要）
    recent_tokens = _count_tokens(_messages_to_text(recent_msgs))
    older_tokens = _count_tokens(_messages_to_text(older_msgs))
    existing_summary = ""
    if store is not None and session_id:
        existing_summary, up_to_turn = store.get_summary(session_id)
        # 过滤掉已被旧摘要覆盖的消息，只压缩增量
        if existing_summary and up_to_turn > 0:
            older_msgs = [
                m for m in older_msgs
                if int(m.get("turn_index", 0) or 0) > up_to_turn
            ]

    if older_tokens + recent_tokens <= _MAX_TOKENS and not existing_summary:
        # 未超阈值且无旧摘要：保持原文
        return "", history
    if not older_msgs:
        # 没有新消息需要压缩：直接用旧摘要 + 窗口
        return existing_summary, recent_msgs

    # 触发压缩：把 older_msgs 融合到摘要
    summary = _summarize_with_llm(older_msgs, existing_summary)
    # 持久化（记录覆盖到哪一轮）
    if summary and store is not None and session_id:
        last_turn = max(
            (int(m.get("turn_index", 0) or 0) for m in older_msgs),
            default=0,
        )
        store.set_summary(session_id, summary, last_turn)
    return summary, recent_msgs


def _build_input_messages(
    query: str,
    history: list[dict] | None,
    summary: str = "",
) -> list[dict]:
    """构造 agent 输入消息：[摘要 system] + history + 当前 user。"""
    msgs: list[dict] = []
    if summary:
        msgs.append({
            "role": "system",
            "content": f"以下是先前对话的摘要，供你了解上下文：\n{summary}",
        })
    for h in history or []:
        role = h.get("role")
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": h.get("content", "")})
    msgs.append({"role": "user", "content": query})
    return msgs


@dataclass
class ToolCallRecord:
    """一次工具调用的记录，用于前端展示 Agent 思考过程。"""

    name: str
    args: dict[str, Any]
    result: str  # 工具返回给 LLM 的文本
    sources: list[Document] = field(default_factory=list)  # 命中的来源文档（若有）

    def to_dict(self) -> dict[str, Any]:
        sources_detail = []
        for doc in self.sources:
            meta = doc.metadata or {}
            sources_detail.append({
                "title": meta.get("title") or "未命名",
                "source": meta.get("source") or "",
            })
        return {
            "name": self.name,
            "args": self.args,
            "result_preview": self.result[:300],
            "sources_count": len(self.sources),
            "sources_detail": sources_detail,
        }


@dataclass
class ToolSpec:
    """工具规格（兼容保留）：name + 给 LLM 的 BaseTool。

    create_agent 会自动执行 tool，不再需要手动 execute。
    """

    name: str
    tool: BaseTool


class _RunContext:
    """单次 agent 运行的上下文，收集工具调用时检索到的文档与记录。"""

    def __init__(self) -> None:
        self.collected_docs: list[Document] = []
        self.records: list[ToolCallRecord] = []


def _format_docs(docs: list[Document]) -> str:
    """把召回的文档拼接成给 LLM 看的文本，每条带 [来源N] 标号。"""
    if not docs:
        return "未检索到相关资料。"
    blocks = []
    for i, doc in enumerate(docs, start=1):
        content = doc.page_content.strip()
        title = doc.metadata.get("title") or doc.metadata.get("source") or "未命名"
        blocks.append(f"[来源{i}] (知识库) {title}\n{content}")
    return "\n\n".join(blocks)


def _build_tools_with_context(k: int, ctx: _RunContext) -> list[BaseTool]:
    """构建工具列表，工具执行时把命中文档存入 ctx。"""

    @tool("search_knowledge_base")
    def search_knowledge_base(query: str) -> str:
        """检索本地知识库，返回与查询最相关的若干文档片段。

        适用于用户询问产品规则、制度、文档内容、业务事实等问题。
        若返回"未检索到相关资料"，说明本地知识库未命中，
        此时对于需要最新信息或外部信息的问题，应转用 tavily_web_search 工具。

        Args:
            query: 用于语义检索的关键词或问题。
        """
        store = get_vectorstore()
        docs = store.similarity_search(query, k=k)
        ctx.collected_docs.extend(docs)
        ctx.records.append(
            ToolCallRecord(
                name="search_knowledge_base",
                args={"query": query},
                result=_format_docs(docs),
                sources=docs,
            )
        )
        return _format_docs(docs)

    tools_list: list[BaseTool] = [search_knowledge_base]

    # 网络搜索工具：仅在配置了 Tavily API Key 时启用
    if tavily_enabled():
        @tool("tavily_web_search")
        def tavily_web_search(query: str) -> str:
            """调用 Tavily 进行网络搜索，获取最新或本地知识库未覆盖的信息。

            使用时机：
            - 已调用 search_knowledge_base 但未检索到相关资料（返回"未检索到相关资料"）时；
            - 用户明确询问实时信息、新闻、近期事件、外部产品/技术文档等知识库外的内容时。

            不要用于知识库内已有的事实性问题（如内部制度、产品规则）。

            Args:
                query: 搜索关键词或问题。
            """
            resp = tavily_search(query)
            formatted = tavily_format(resp)
            # 把搜索结果转为 Document 形式，便于前端展示来源
            sources = [
                Document(
                    page_content=r.content,
                    metadata={"title": r.title, "source": r.url},
                )
                for r in resp.results
            ]
            ctx.collected_docs.extend(sources)
            ctx.records.append(
                ToolCallRecord(
                    name="tavily_web_search",
                    args={"query": query},
                    result=formatted,
                    sources=sources,
                )
            )
            return formatted

        tools_list.append(tavily_web_search)

    return tools_list


def get_tool_specs(k: int = 4) -> list[ToolSpec]:
    """返回所有可用工具的 ToolSpec（兼容旧调用方）。"""
    ctx = _RunContext()
    return [ToolSpec(name=t.name, tool=t) for t in _build_tools_with_context(k, ctx)]


# 默认系统提示词（知识库问答助手，供 /api/search/agent 使用）
_AGENT_SYSTEM_PROMPT = """你是一个严谨的中文知识库问答助手。

你可用的工具：
- `search_knowledge_base`：检索本地知识库（产品规则、制度、文档内容、业务事实等）。
- `tavily_web_search`：调用 Tavily 进行网络搜索（仅在知识库未命中、或用户询问实时/外部信息时使用）。

请遵循以下规则：
1. 涉及知识库中的事实、制度、产品规则、业务条款等问题，**必须先调用** search_knowledge_base 检索。
2. 若 search_knowledge_base 返回"未检索到相关资料"，且问题确实需要外部信息（如最新动态、新闻、外部产品文档），则**再调用** tavily_web_search 搜索网络；不需要外部信息时，直接告知"根据当前知识库，我无法回答该问题"。
3. 打招呼、闲聊、通用常识问题可直接回答，无需调用工具。
4. 严格依据检索/搜索到的资料作答；资料不足以回答时如实告知。
5. 引用资料时用 [来源1] [来源2] 等数字编号标注在对应内容后面，不要使用 [来源N] [来源M] 等字母。
"""


def _make_agent(
    k: int,
    system_prompt: str | None,
    tools: list[ToolSpec] | None,
    ctx: _RunContext,
):
    """构建 create_agent 实例。

    历史压缩在调用前由 _compress_history 处理（滑动窗口 + token 阈值 + 前置摘要），
    agent 本身保持无状态，便于横向扩展与可控性。
    """
    llm = get_llm()
    if tools is not None:
        tool_list = [s.tool for s in tools]
    else:
        tool_list = _build_tools_with_context(k, ctx)
    base_prompt = system_prompt or _AGENT_SYSTEM_PROMPT
    return create_agent(
        llm,
        tools=tool_list,
        system_prompt=base_prompt,
    )


def run_agent(
    query: str,
    history: list[dict[str, str]] | None = None,
    k: int = 4,
    system_prompt: str | None = None,
    tools: list[ToolSpec] | None = None,
    session_id: str | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """运行 tool-calling agent（基于 create_agent），让大模型自主决定是否调用工具。

    会话记忆与压缩：
    - 传 session_id 时，从 SQLite 读取全部历史，经 _compress_history（滑动窗口 +
      token 阈值 + 前置摘要）压缩后作为输入；运行结束后把本轮对话写回 SQLite。
      摘要持久化在 SQLite，下次复用做增量压缩。
    - 未传 session_id 时，使用传入的 history（内存历史，同样走压缩，但不持久化）。

    Args:
        query: 当前用户问题。
        history: 多轮对话历史（无 session_id 时使用），每项 {"role":..., "content":...}。
        k: 检索召回数量（仅对检索类工具有效）。
        system_prompt: 自定义系统提示词；不传则用默认的 _AGENT_SYSTEM_PROMPT。
        tools: 自定义工具列表；不传则用默认知识库检索工具。
        session_id: 会话 id；传入则启用 SQLite 持久化记忆与摘要增量更新。
        user_id: 用户 id；用于会话归属校验与持久化（登录用户或访客 id）。

    Returns:
        dict: {
            "answer": str,
            "source_documents": list[Document],
            "tool_call_records": list[dict],
            "tool_calls": int,
            "session_id": str | None,
        }
    """
    ctx = _RunContext()
    store = get_chat_store() if session_id else None

    # 读取全部历史
    if session_id:
        full_history = store.get_all(session_id)
    else:
        full_history = [
            {"role": h["role"], "content": h.get("content", "")}
            for h in (history or [])
            if h.get("role") in ("user", "assistant")
        ]

    # 压缩历史：滑动窗口 + token 阈值 + 前置摘要
    summary, recent_history = _compress_history(full_history, store, session_id)

    agent = _make_agent(k, system_prompt, tools, ctx)
    input_msgs = _build_input_messages(query, recent_history, summary)

    result = agent.invoke({"messages": input_msgs})

    # 从最后一条 AIMessage 提取答案
    answer = ""
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage):
            answer = m.content or ""
            break

    # 写回本轮对话到 SQLite
    if session_id and store is not None and answer:
        turn_index = store.count_turns(session_id) + 1
        store.add_turn(session_id, turn_index, query, answer, user_id=user_id)

    return {
        "answer": answer,
        "source_documents": ctx.collected_docs,
        "tool_call_records": [r.to_dict() for r in ctx.records],
        "tool_calls": len(ctx.records),
        "session_id": session_id,
    }


def run_agent_stream(
    query: str,
    history: list[dict[str, str]] | None = None,
    k: int = 4,
    system_prompt: str | None = None,
    tools: list[ToolSpec] | None = None,
    session_id: str | None = None,
    user_id: str = "",
):
    """流式版 tool-calling agent（基于 create_agent）。

    会话记忆与压缩同 run_agent：传 session_id 则从 SQLite 读取全部历史并
    增量压缩；否则用传入的 history（内存历史）。

    生成器，依次 yield 事件 dict：
      {"type": "tool_call", "data": {...}}          每次工具调用
      {"type": "token", "data": "片段"}             最终答案的 token 增量
      {"type": "sources", "data": [Document, ...]}  所有命中来源（token 之前发）
      {"type": "done", "data": {"tool_calls": N, "session_id": ...}}  结束
      {"type": "error", "data": "msg"}              出错
    """
    ctx = _RunContext()
    store = get_chat_store() if session_id else None

    # 读取全部历史
    if session_id:
        full_history = store.get_all(session_id)
    else:
        full_history = [
            {"role": h["role"], "content": h.get("content", "")}
            for h in (history or [])
            if h.get("role") in ("user", "assistant")
        ]

    # 压缩历史：滑动窗口 + token 阈值 + 前置摘要
    summary, recent_history = _compress_history(full_history, store, session_id)

    agent = _make_agent(k, system_prompt, tools, ctx)
    input_msgs = _build_input_messages(query, recent_history, summary)

    answer_parts: list[str] = []
    try:
        last_record_count = 0
        for chunk, _ in agent.stream({"messages": input_msgs}, stream_mode="messages"):
            if isinstance(chunk, AIMessageChunk):
                # 工具调用阶段的 chunk（含 tool_call_chunks）不输出 token
                tcc = getattr(chunk, "tool_call_chunks", None)
                if tcc:
                    continue
                content = getattr(chunk, "content", "") or ""
                if content:
                    answer_parts.append(content)
                    yield {"type": "token", "data": content}
            elif isinstance(chunk, ToolMessage):
                # 工具执行完成，输出新增的调用记录
                while last_record_count < len(ctx.records):
                    yield {
                        "type": "tool_call",
                        "data": ctx.records[last_record_count].to_dict(),
                    }
                    last_record_count += 1
        # 写回本轮对话到 SQLite
        if session_id and store is not None and answer_parts:
            answer = "".join(answer_parts).strip()
            if answer:
                turn_index = store.count_turns(session_id) + 1
                store.add_turn(session_id, turn_index, query, answer, user_id=user_id)
        yield {"type": "sources", "data": ctx.collected_docs}
        yield {
            "type": "done",
            "data": {"tool_calls": len(ctx.records), "session_id": session_id},
        }
    except Exception as e:
        yield {"type": "error", "data": str(e)[:300]}
