"""股票研报工具集：将 stock_data 封装为 LangChain Tool，供 Agent 调用。

设计要点：
1. 复用 tools.py 的 _RunContext 模式。
2. 工具返回文本给 LLM，收集工具调用记录供 SSE tool_call 事件推送。
3. run_stock_agent / run_stock_agent_stream 复用 tools.py 的历史压缩与流式逻辑。
4. 双模式：股票模式（股票分析师）+ 万能助手模式（非股票问题用 Tavily 联网搜索）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import BaseTool, tool

from app.rag.chat_memory import get_chat_store
from app.rag.llm import get_llm
from app.rag.stock_data import (
    get_financial_indicators,
    get_money_flow,
    get_stock_kline,
    get_stock_news,
    get_stock_quote,
    normalize_code,
    search_stock_code,
)
from app.rag.tavily_search import is_enabled as tavily_enabled
from app.rag.tavily_search import format_results as tavily_format
from app.rag.tavily_search import search as tavily_search
from app.rag.tools import _compress_history, _build_input_messages, ToolCallRecord, _format_docs
from app.rag.vectorstore import get_vectorstore
from langchain_core.documents import Document


@dataclass
class StockRunContext:
    """股票 agent 运行上下文，收集工具调用记录。"""

    records: list[ToolCallRecord] = field(default_factory=list)
    collected_docs: list[Document] = field(default_factory=list)
    stock_code: str = ""
    stock_name: str = ""


# —— PDF 生成意图检测：用户明确要求生成 PDF/下载报告 ——
_PDF_INTENT_RE = re.compile(
    r"(生成.*pdf|生成.*PDF|pdf.*生成|PDF.*生成|"
    r"下载.*报告|下载.*研报|报告.*下载|研报.*下载|"
    r"导出.*pdf|导出.*PDF|pdf.*导出|PDF.*导出|"
    r"保存为pdf|保存为PDF|存成pdf|存成PDF|"
    r"生成.*文档|生成.*报告|文档.*生成|报告.*生成|"
    r"下载.*pdf|下载.*PDF)",
    flags=re.IGNORECASE,
)

# —— 问题分类：判断是股票相关还是非股票问题 ——
_STOCK_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_STOCK_KEYWORDS = re.compile(
    r"(股票|股市|大盘|指数|行情|K线|日线|周线|月线|涨停|跌停|"
    r"涨停板|跌停板|牛市|熊市|震荡|回调|反弹|突破|"
    r"市盈率|市净率|市值|股价|个股|板块|行业|概念股|龙头股|"
    r"A股|港股|美股|创业板|科创板|主板|中小板|"
    r"基金|债券|期货|期权|可转债|融资|融券|"
    r"分红|配股|增发|IPO|上市|退市|"
    r"财报|季报|年报|业绩|营收|利润|ROE|EPS|"
    r"资金流向|主力|大单|散户|筹码|"
    r"买入|卖出|持仓|建仓|平仓|加仓|减仓|"
    r"证券|券商|基金经理|投资顾问|"
    r"茅台|平安银行|贵州|招商银行|五粮液|"
    r"分析|研报|预测|估值|评级|目标价)"
)


def _classify_query(query: str) -> str:
    """判断问题类型：'stock' 或 'non_stock'。

    策略：
    1. 含 6 位数字代码 → 股票
    2. 含股票关键词 → 股票
    3. 其他 → 非股票（万能助手模式）
    """
    if _STOCK_CODE_RE.search(query):
        return "stock"
    if _STOCK_KEYWORDS.search(query):
        return "stock"
    return "non_stock"


# —— 万能助手模式提示词（非股票问题时切换使用） ——
_GENERAL_ASSISTANT_PROMPT = """你是一个万能智能助手，可以回答各种领域的问题。

你可以使用以下工具：
- tavily_web_search：搜索互联网，获取最新资讯、新闻、实时信息、外部知识等。
- search_knowledge_base：检索本地知识库，获取企业内部资料、制度文档等。

回答规则：
1. **必须使用工具**来回答问题——不要凭记忆编造答案。
2. 对于需要外部信息的问题（如最新新闻、时事动态、天气、体育、娱乐、科技等），**必须调用 tavily_web_search** 搜索网络后再回答。
3. 对于内部知识问题（如企业制度、产品规则），先调用 search_knowledge_base 检索。
4. 对于常识性问题，如果确定不需要外部信息，可以直接回答；但如果涉及时效性，仍应调用 tavily_web_search。
5. 搜索到的结果要认真阅读和理解，用自己的语言组织成通顺的回答。
6. 引用资料时用 [来源1] [来源2] 等数字编号标注在对应内容后面。
7. **不要在回答末尾手动列出参考来源链接**，系统会自动在结尾附加参考来源区块，你只需专注正文内容的组织。
8. 回答风格：友好、专业、简洁，使用中文。"""


def _build_stock_tools(ctx: StockRunContext) -> list[BaseTool]:
    """构建股票数据工具列表。"""

    @tool("search_stock_code")
    def search_stock_code_tool(keyword: str) -> str:
        """按股票名称/简称/拼音首字母搜索 A 股代码。

        当用户输入的是股票名称（如"中国铝业"、"贵州茅台"）或拼音首字母（如 zgly）
        而不是 6 位数字代码时，必须先调用本工具解析出正确的股票代码，
        再用解析出的代码调用 get_stock_quote / get_stock_kline 等其他工具。
        严禁凭记忆猜测代码。

        Args:
            keyword: 股票名称、简称或拼音首字母（如 中国铝业 / zgly）。
        """
        result = search_stock_code(keyword)
        if "error" in result:
            msg = f"[无需重试] {result['error']} 请直接基于已有数据回答。"
            ctx.records.append(ToolCallRecord(
                name="search_stock_code", args={"keyword": keyword}, result=msg
            ))
            return msg

        matches = result.get("matches", [])
        lines = [f"  {m['code']}  {m['name']}（{m['market']}）" for m in matches]
        text = (
            f"搜索「{keyword}」匹配的 A 股：\n"
            + "\n".join(lines)
            + "\n请从中选择与用户意图最匹配的股票，用其 6 位代码继续调用行情/财务等工具。"
        )
        ctx.records.append(ToolCallRecord(
            name="search_stock_code", args={"keyword": keyword}, result=text
        ))
        return text

    @tool("get_stock_quote")
    def get_stock_quote_tool(code: str) -> str:
        """获取股票实时行情数据。

        包括：当前价格、涨跌幅、成交量、成交额、开盘价、最高价、最低价、
        换手率、市盈率(PE)、市净率(PB)、总市值、流通市值等。

        Args:
            code: 股票代码，6位数字（如 000001、600519）。
        """
        result = get_stock_quote(code)
        if "error" in result:
            msg = f"[无需重试] {result['error']} 请直接基于已有数据回答。"
            ctx.records.append(ToolCallRecord(
                name="get_stock_quote", args={"code": code}, result=msg
            ))
            return msg

        ctx.stock_code = result.get("code", code)
        ctx.stock_name = result.get("name", "")

        # 给 LLM 的文本摘要
        text = (
            f"股票：{result['name']}（{result['code']}）\n"
            f"最新价：{result['price']}\n"
            f"涨跌额：{result['change']}  涨跌幅：{result['change_pct']}%\n"
            f"今开：{result['open']}  最高：{result['high']}  最低：{result['low']}\n"
            f"昨收：{result['prev_close']}\n"
            f"成交量：{result['volume']:.0f}手  成交额：{result['amount']:.0f}元\n"
            f"换手率：{result['turnover_rate']}%\n"
            f"PE(动态)：{result['pe']}  PB：{result['pb']}\n"
            f"总市值：{result['total_mv']:.0f}元  流通市值：{result['circ_mv']:.0f}元"
        )
        ctx.records.append(ToolCallRecord(
            name="get_stock_quote", args={"code": code}, result=text
        ))
        return text

    @tool("get_stock_kline")
    def get_stock_kline_tool(code: str, period: str = "daily", count: int = 60) -> str:
        """获取股票K线数据（OHLCV）及技术指标。

        返回最近 N 个周期的开盘价、收盘价、最高价、最低价、成交量，
        并计算 MA5/MA10/MA20/MA60 均线、MACD、KDJ 技术指标。

        Args:
            code: 股票代码，6位数字。
            period: K线周期，可选 daily(日线)/weekly(周线)/monthly(月线)，默认 daily。
            count: 获取的K线条数，默认 60。
        """
        result = get_stock_kline(code, period, count)
        if "error" in result:
            msg = f"[无需重试] {result['error']} 请直接基于已有数据回答。"
            ctx.records.append(ToolCallRecord(
                name="get_stock_kline", args={"code": code, "period": period, "count": count},
                result=msg
            ))
            return msg

        # 给 LLM 的文本摘要：最近 5 日数据 + 指标信号
        kline = result.get("kline", [])
        dates = result.get("dates", [])
        ma = result.get("ma", {})

        recent_lines = []
        for i in range(max(0, len(kline) - 5), len(kline)):
            d, o, c, l, h = kline[i]
            vol = result["volume"][i][1] if i < len(result.get("volume", [])) else 0
            recent_lines.append(f"  {d}: 开{o} 收{c} 低{l} 高{h} 量{vol:.0f}")

        # 均线最新值
        ma_lines = []
        for n in (5, 10, 20, 60):
            key = f"ma{n}"
            vals = ma.get(key, [])
            if vals and vals[-1] is not None:
                ma_lines.append(f"MA{n}={vals[-1]}")

        # MACD 信号
        macd = result.get("macd", {})
        macd_signal = ""
        if macd.get("dif") and macd.get("dea"):
            dif_last = macd["dif"][-1]
            dea_last = macd["dea"][-1]
            if dif_last > dea_last:
                macd_signal = "MACD 金叉（多头信号）"
            else:
                macd_signal = "MACD 死叉（空头信号）"

        # KDJ 信号
        kdj = result.get("kdj", {})
        kdj_signal = ""
        if kdj.get("k") and kdj.get("d"):
            k_last = kdj["k"][-1]
            d_last = kdj["d"][-1]
            if k_last > 80:
                kdj_signal = "KDJ 超买区域"
            elif k_last < 20:
                kdj_signal = "KDJ 超卖区域"
            elif k_last > d_last:
                kdj_signal = "KDJ 金叉"
            else:
                kdj_signal = "KDJ 死叉"

        text = (
            f"K线数据（{period}，最近{len(kline)}条）：\n"
            + "\n".join(recent_lines) + "\n"
            f"均线：{', '.join(ma_lines)}\n"
            f"{macd_signal}\n"
            f"{kdj_signal}"
        )
        ctx.records.append(ToolCallRecord(
            name="get_stock_kline", args={"code": code, "period": period, "count": count},
            result=text
        ))
        return text

    @tool("get_financial_data")
    def get_financial_data_tool(code: str) -> str:
        """获取股票财务指标数据。

        包括：ROE、营业净利率、毛利率、营收同比增长、净利润同比增长等。
        返回最近两期数据，可用于对比分析。

        Args:
            code: 股票代码，6位数字。
        """
        result = get_financial_indicators(code)
        if "error" in result:
            msg = f"[无需重试] {result['error']} 请直接基于已有数据回答。"
            ctx.records.append(ToolCallRecord(
                name="get_financial_data", args={"code": code}, result=msg
            ))
            return msg

        indicators = result.get("indicators", [])
        current = result.get("current", [])
        previous = result.get("previous", [])
        report_dates = result.get("report_dates", [])

        lines = []
        for i, ind in enumerate(indicators):
            cur = current[i] if i < len(current) else 0
            prev = previous[i] if i < len(previous) else 0
            lines.append(f"  {ind}: 本期{cur:.2f}%, 上期{prev:.2f}%")

        text = (
            f"财务指标（{code}）：\n"
            f"报告期：{', '.join(report_dates[:2])}\n"
            + "\n".join(lines)
        )
        ctx.records.append(ToolCallRecord(
            name="get_financial_data", args={"code": code}, result=text
        ))
        return text

    @tool("get_money_flow")
    def get_money_flow_tool(code: str, days: int = 10) -> str:
        """获取股票资金流向数据。

        包括：主力净流入、超大单净流入、大单净流入、中单净流入、小单净流入。
        返回最近 N 日数据，用于判断资金动向。

        Args:
            code: 股票代码，6位数字。
            days: 获取天数，默认 10。
        """
        result = get_money_flow(code, days)
        if "error" in result:
            msg = f"[无需重试] {result['error']} 请直接基于已有数据回答。"
            ctx.records.append(ToolCallRecord(
                name="get_money_flow", args={"code": code, "days": days}, result=msg
            ))
            return msg

        dates = result.get("dates", [])
        main_flow = result.get("main_flow", [])

        # 汇总：近 N 日主力净流入总和
        total_main = sum(main_flow) if main_flow else 0
        direction = "净流入" if total_main > 0 else "净流出"

        # 最近 5 日明细
        recent_lines = []
        for i in range(max(0, len(dates) - 5), len(dates)):
            mf = main_flow[i] if i < len(main_flow) else 0
            d = dates[i]
            arrow = "↑" if mf > 0 else "↓"
            recent_lines.append(f"  {d}: 主力{arrow}{abs(mf):.0f}元")

        text = (
            f"资金流向（{code}，最近{len(dates)}日）：\n"
            f"主力累计{direction}：{abs(total_main):.0f}元\n"
            + "\n".join(recent_lines)
        )
        ctx.records.append(ToolCallRecord(
            name="get_money_flow", args={"code": code, "days": days}, result=text
        ))
        return text

    @tool("get_stock_news")
    def get_stock_news_tool(code: str, stock_name: str = "") -> str:
        """获取个股最新新闻舆情（东方财富+新浪财经实时数据）。

        返回最新财经新闻的标题、链接和摘要，用于判断市场情绪和热点事件。

        Args:
            code: 股票代码，6位数字。
            stock_name: 股票名称（如知道则传入，提高搜索准确性）。
        """
        result = get_stock_news(code, stock_name)
        if "error" in result:
            msg = f"[无需重试] {result['error']} 请直接基于已有数据回答。"
            ctx.records.append(ToolCallRecord(
                name="get_stock_news", args={"code": code, "stock_name": stock_name},
                result=msg
            ))
            return msg

        news = result.get("news", [])

        lines = []
        news_docs = []
        for i, n in enumerate(news, 1):
            lines.append(f"  [{i}] {n['title']}\n      {n['content'][:100]}...")
            url = n.get("url", "")
            if url:
                news_docs.append(Document(
                    page_content=n.get("content", "")[:200],
                    metadata={"title": n.get("title", "未命名"), "source": url},
                ))

        text = f"最新新闻（{stock_name or code}）：\n" + "\n".join(lines)
        if news_docs:
            ctx.collected_docs.extend(news_docs)
        ctx.records.append(ToolCallRecord(
            name="get_stock_news", args={"code": code, "stock_name": stock_name},
            result=text, sources=news_docs,
        ))
        return text

    @tool("search_knowledge_base")
    def search_knowledge_base_tool(query: str) -> str:
        """检索本地企业知识库，返回与查询最相关的若干文档片段。

        适用于查询企业内部资料、产品规则、制度文档、行业研究、业务事实等。
        当需要补充企业背景、行业研究报告、政策文件等内容时调用。

        Args:
            query: 用于语义检索的关键词或问题。
        """
        store = get_vectorstore()
        docs = store.similarity_search(query, k=4)
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

    tools_list: list[BaseTool] = [
        search_stock_code_tool,
        get_stock_quote_tool,
        get_stock_kline_tool,
        get_financial_data_tool,
        get_money_flow_tool,
        get_stock_news_tool,
        search_knowledge_base_tool,
    ]

    # 网络搜索工具：仅在配置了 Tavily API Key 时启用，用于回答非股票类问题
    if tavily_enabled():

        @tool("tavily_web_search")
        def tavily_web_search_tool(query: str) -> str:
            """调用 Tavily 网络搜索，获取最新或外部信息。

            使用时机：当用户的问题与股票/个股分析无关时（如新闻、天气、
            最新事件、外部知识、通用常识等），本地知识库和股票数据工具都无法
            回答，此时改用本工具搜索网络作答。

            Args:
                query: 搜索关键词或问题。
            """
            resp = tavily_search(query)
            formatted = tavily_format(resp)
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

        tools_list.append(tavily_web_search_tool)

    return tools_list


def _build_general_tools(ctx: StockRunContext) -> list[BaseTool]:
    """构建万能助手模式的工具列表（Tavily 搜索 + 知识库检索）。"""

    @tool("search_knowledge_base")
    def search_knowledge_base_tool(query: str) -> str:
        """检索本地企业知识库，返回与查询最相关的若干文档片段。

        适用于查询企业内部资料、产品规则、制度文档、行业研究、业务事实等。

        Args:
            query: 用于语义检索的关键词或问题。
        """
        store = get_vectorstore()
        docs = store.similarity_search(query, k=4)
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

    tools_list: list[BaseTool] = [search_knowledge_base_tool]

    if tavily_enabled():

        @tool("tavily_web_search")
        def tavily_web_search_tool(query: str) -> str:
            """调用 Tavily 网络搜索，获取最新或外部信息。

            当用户询问最新新闻、时事动态、天气、科技、体育、娱乐等
            需要外部实时信息的问题时，必须调用本工具搜索网络作答。

            Args:
                query: 搜索关键词或问题。
            """
            resp = tavily_search(query)
            formatted = tavily_format(resp)
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

        tools_list.append(tavily_web_search_tool)

    return tools_list


def _make_agent(system_prompt: str, ctx: StockRunContext, mode: str = "stock"):
    """构建 agent，根据 mode 选择不同的工具集。

    Args:
        mode: 'stock' 股票分析师模式 | 'general' 万能助手模式
    """
    llm = get_llm()
    if mode == "general":
        tool_list = _build_general_tools(ctx)
    else:
        tool_list = _build_stock_tools(ctx)
    return create_agent(llm, tools=tool_list, system_prompt=system_prompt)


def _resolve_agent_params(query: str, system_prompt: str) -> tuple[str, str]:
    """根据问题类型确定 agent 模式和提示词。

    Returns:
        (mode, effective_prompt)
        mode: 'stock' 或 'general'
    """
    cls = _classify_query(query)
    mode = "general" if cls == "non_stock" else "stock"
    if mode == "general":
        return mode, _GENERAL_ASSISTANT_PROMPT
    return mode, system_prompt


def run_stock_agent(
    query: str,
    system_prompt: str,
    history: list[dict[str, str]] | None = None,
    session_id: str | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """运行 agent（非流式），自动根据问题类型切换模式。"""
    mode, effective_prompt = _resolve_agent_params(query, system_prompt)
    ctx = StockRunContext()
    store = get_chat_store() if session_id else None

    if session_id:
        full_history = store.get_all(session_id)
    else:
        full_history = [
            {"role": h["role"], "content": h.get("content", "")}
            for h in (history or [])
            if h.get("role") in ("user", "assistant")
        ]

    summary, recent_history = _compress_history(full_history, store, session_id)
    agent = _make_agent(effective_prompt, ctx, mode=mode)
    input_msgs = _build_input_messages(query, recent_history, summary)

    result = agent.invoke({"messages": input_msgs})

    answer = ""
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage):
            answer = m.content or ""
            break

    if session_id and store is not None and answer:
        # 收集 metadata
        meta_sources = []
        for record in ctx.records:
            for doc in record.sources:
                dmeta = doc.metadata or {}
                url = dmeta.get("source") or ""
                title = dmeta.get("title") or "未命名"
                if url and url.startswith("http"):
                    meta_sources.append({"title": title, "source": url})
        for doc in ctx.collected_docs:
            dmeta = doc.metadata or {}
            src = dmeta.get("source") or "knowledge_base"
            if not (isinstance(src, str) and src.startswith("http")):
                meta_sources.append({
                    "title": dmeta.get("title") or src or "未命名",
                    "source": "knowledge_base",
                })
        tool_data_summary = [
            {"tool": r.name, "input": (r.tool_input or "")[:120], "output": str(r.output or "")[:200]}
            for r in ctx.records if r.output is not None
        ]
        assistant_meta = {
            "sources": meta_sources,
            "tool_data": tool_data_summary,
            "stock_code": ctx.stock_code,
            "stock_name": ctx.stock_name,
            "mode": mode,
        }
        turn_index = store.count_turns(session_id) + 1
        store.add_turn(
            session_id, turn_index, query, answer,
            user_id=user_id, assistant_metadata=assistant_meta,
        )

    return {
        "answer": answer,
        "tool_call_records": [r.to_dict() for r in ctx.records],
        "stock_code": ctx.stock_code,
        "stock_name": ctx.stock_name,
        "tool_calls": len(ctx.records),
        "session_id": session_id,
        "mode": mode,
    }


def run_stock_agent_stream(
    query: str,
    system_prompt: str,
    history: list[dict[str, str]] | None = None,
    session_id: str | None = None,
    user_id: str = "",
):
    """流式版 agent，自动根据问题类型切换模式。

    生成器，依次 yield 事件 dict：
      {"type": "tool_call", "data": {...}}    每次工具调用
      {"type": "token", "data": "片段"}       最终答案的 token 增量
      {"type": "sources", "data": [...]}      来源（如有）
      {"type": "done", "data": {...}}         结束
      {"type": "error", "data": "msg"}        出错
    """
    mode, effective_prompt = _resolve_agent_params(query, system_prompt)
    ctx = StockRunContext()
    store = get_chat_store() if session_id else None

    if session_id:
        full_history = store.get_all(session_id)
    else:
        full_history = [
            {"role": h["role"], "content": h.get("content", "")}
            for h in (history or [])
            if h.get("role") in ("user", "assistant")
        ]

    summary, recent_history = _compress_history(full_history, store, session_id)
    agent = _make_agent(effective_prompt, ctx, mode=mode)
    input_msgs = _build_input_messages(query, recent_history, summary)

    answer_parts: list[str] = []
    MAX_TOOL_CALLS = 15
    try:
        last_record_count = 0
        for chunk, _ in agent.stream({"messages": input_msgs}, stream_mode="messages"):
            if len(ctx.records) >= MAX_TOOL_CALLS:
                break
            if isinstance(chunk, AIMessageChunk):
                tcc = getattr(chunk, "tool_call_chunks", None)
                if tcc:
                    continue
                content = getattr(chunk, "content", "") or ""
                if content:
                    answer_parts.append(content)
                    yield {"type": "token", "data": content}
            elif isinstance(chunk, ToolMessage):
                while last_record_count < len(ctx.records):
                    yield {
                        "type": "tool_call",
                        "data": ctx.records[last_record_count].to_dict(),
                    }
                    last_record_count += 1

        # —— 自动检测 PDF 生成意图（提前计算，供 metadata 与后续生成共用） ——
        want_pdf = bool(_PDF_INTENT_RE.search(query or ""))

        if session_id and store is not None and answer_parts:
            answer = "".join(answer_parts).strip()
            if answer:
                # 收集 sources 和工具数据摘要，存入 metadata 供 PDF 导出使用
                meta_sources = []
                for record in ctx.records:
                    for doc in record.sources:
                        dmeta = doc.metadata or {}
                        url = dmeta.get("source") or ""
                        title = dmeta.get("title") or "未命名"
                        if url and url.startswith("http"):
                            meta_sources.append({"title": title, "source": url})
                for doc in ctx.collected_docs:
                    dmeta = doc.metadata or {}
                    src = dmeta.get("source") or "knowledge_base"
                    if not (isinstance(src, str) and src.startswith("http")):
                        meta_sources.append({
                            "title": dmeta.get("title") or src or "未命名",
                            "source": "knowledge_base",
                        })
                # 工具数据摘要
                tool_data_summary = []
                for record in ctx.records:
                    if not record.result:
                        continue
                    tool_data_summary.append({
                        "tool": record.name,
                        "input": str(record.args)[:120] if record.args else "",
                        "output": record.result[:200],
                    })
                assistant_meta = {
                    "sources": meta_sources,
                    "tool_data": tool_data_summary,
                    "stock_code": ctx.stock_code,
                    "stock_name": ctx.stock_name,
                    "mode": mode,
                    "pdf_requested": want_pdf,
                }
                turn_index = store.count_turns(session_id) + 1
                store.add_turn(
                    session_id, turn_index, query, answer,
                    user_id=user_id, assistant_metadata=assistant_meta,
                )

        # 收集所有带 URL 的来源（Tavily 搜索 + 新闻链接 + 知识库），通过 sources 事件传给前端渲染
        all_refs = []
        seen_urls = set()
        for record in ctx.records:
            for doc in record.sources:
                meta = doc.metadata or {}
                url = meta.get("source") or ""
                title = meta.get("title") or "未命名"
                if url and url.startswith("http") and url not in seen_urls:
                    seen_urls.add(url)
                    all_refs.append({"title": title, "url": url, "tool": record.name})

        sources_data = []
        # 优先放带 URL 的来源（可点击）
        for ref in all_refs:
            sources_data.append({
                "title": ref["title"],
                "content": "",
                "source": ref["url"],
            })
        # 再放知识库文档（无 URL 的本地文档）
        for doc in ctx.collected_docs:
            meta = doc.metadata or {}
            src = meta.get("source") or "knowledge_base"
            if isinstance(src, str) and src.startswith("http"):
                continue  # 已在 all_refs 中
            sources_data.append({
                "title": meta.get("title") or src or "未命名",
                "content": doc.page_content[:200],
                "source": "knowledge_base",
            })
        yield {"type": "sources", "data": sources_data}

        # —— 自动生成 PDF（仅当用户明确要求时） ——
        pdf_result_data: dict[str, Any] | None = None
        answer_text = "".join(answer_parts).strip()
        if want_pdf and answer_text:
            # 把 sources_data 转成 pdf_report 需要的格式（title / source）
            pdf_sources = [
                {
                    "title": s.get("title") or "未命名",
                    "source": s.get("source") or "",
                }
                for s in sources_data
            ]
            # 收集工具数据摘要
            pdf_tool_data = []
            for record in ctx.records:
                if not record.result:
                    continue
                pdf_tool_data.append({
                    "tool": record.name,
                    "input": str(record.args)[:120] if record.args else "",
                    "output": record.result[:200],
                })
            try:
                from app.rag.pdf_report import generate_pdf_report as _gen_pdf

                result = _gen_pdf(
                    title="",
                    stock_code=ctx.stock_code,
                    stock_name=ctx.stock_name,
                    content_md=answer_text,
                    sources=pdf_sources,
                    tool_data=pdf_tool_data,
                )
                pdf_result_data = {
                    "file_name": result.file_name,
                    "download_url": result.download_url,
                    "file_size_kb": result.file_size_kb,
                    "stock_code": ctx.stock_code,
                    "stock_name": ctx.stock_name,
                }
                yield {"type": "pdf_ready", "data": pdf_result_data}
            except Exception as pdf_e:
                # PDF 生成失败不影响主流程，done 里带错误说明
                pdf_result_data = {"error": f"PDF 生成失败：{str(pdf_e)[:200]}"}
                yield {"type": "pdf_ready", "data": pdf_result_data}

        done_data = {
            "tool_calls": len(ctx.records),
            "session_id": session_id,
            "stock_code": ctx.stock_code,
            "stock_name": ctx.stock_name,
            "mode": mode,
        }
        if pdf_result_data is not None:
            done_data["pdf"] = pdf_result_data
        yield {"type": "done", "data": done_data}
    except Exception as e:
        yield {"type": "error", "data": str(e)[:300]}
