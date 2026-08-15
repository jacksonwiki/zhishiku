# Proposal: 股票研报助手智能体

## Why

当前项目是基于 RAG 的通用知识库问答系统，核心能力是「文档检索 + LLM 回答」。用户需要将其改造为**股票研报助手**，具备：

1. **实时行情数据获取**：调用股票数据 API 获取行情、K线、财务指标等结构化数据。
2. **动态提示词**：不同分析场景（快速分析/深度研报/行业对比）使用不同 system prompt，支持 CRUD 管理。
3. **结构化研报生成**：输出包含行情摘要、基本面、技术面、新闻舆情、预测建议的结构化报告。
4. **可视化图表**：研报内嵌 K 线图、走势图、技术指标图、资金流向图、分析流程图等多种图表。
5. **生产级**：完整的用户系统、会话管理、错误处理、流式输出、前端渲染。

现有架构（FastAPI + LangChain Agent + SQLite + Chroma）提供了良好基础：
- Agent tool-calling 机制可直接扩展新工具（股票数据 API）
- 用户认证与会话管理已完备，可直接复用
- 流式 SSE 输出与前端 Markdown 渲染已就绪
- Tavily 网络搜索已集成，可用于新闻舆情

## What Changes

### 新增模块

| 模块 | 说明 |
|------|------|
| `app/rag/stock_data.py` | 股票数据引擎：封装 AkShare，提供行情/K线/财务指标/资金流等接口 |
| `app/rag/stock_tools.py` | 股票工具集：将 stock_data 封装为 LangChain Tool，供 Agent 调用 |
| `app/rag/prompt_manager.py` | 动态提示词管理：SQLite 存储 prompt 模板，支持 CRUD + 变量插值 |
| `app/api/stock_research.py` | 研报 API：对话接口 + 提示词管理接口 + 报告导出 |
| `app/static/stock_research.html` | 研报前端：股票搜索、对话交互、结构化报告渲染 |
| `app/static/stock_research.js` | 前端逻辑：SSE 流式接收、报告渲染、ECharts 图表渲染、Mermaid 流程图 |

### 修改模块

| 模块 | 改动 |
|------|------|
| `app/config.py` | 新增 AkShare 相关配置项 |
| `app/main.py` | 注册研报路由 + 研报页面 |
| `app/api/schemas.py` | 新增研报相关请求/响应模型 |
| `app/rag/tools.py` | 导出 `_compress_history` / `_build_input_messages` 供研报复用 |
| `app/rag/chat_memory.py` | sessions 表新增 `prompt_id` 字段（关联使用的提示词模板） |

### 不变模块（直接复用）

- `app/rag/llm.py` — LLM 封装
- `app/rag/embeddings.py` — Embedding 封装
- `app/rag/vectorstore.py` — Chroma 向量库
- `app/rag/user_store.py` — 用户系统
- `app/api/auth.py` — 认证系统
- `app/rag/tavily_search.py` — 网络搜索（用于新闻舆情）

## Impact

- **新增文件**：6 个（stock_data.py, stock_tools.py, prompt_manager.py, stock_research.py, stock_research.html, stock_research.js）
- **修改文件**：5 个（config.py, main.py, schemas.py, tools.py, chat_memory.py）
- **新增依赖**：`akshare`（中国股票数据 SDK）
- **前端 CDN**：ECharts 5（K线/走势/指标图）、Mermaid.js（流程图/决策树）
- **SSE 协议扩展**：新增 `chart` 事件类型，工具返回的图表数据通过 SSE 推送到前端渲染
- **数据库**：新增 `prompt_templates` 表；sessions 表加 `prompt_id` 列
- **API**：新增 `/api/stock_research/*` 路由组（对话/流式/提示词CRUD/报告列表）
- **前端**：新增研报页面路由 `/stock_research`
- **兼容性**：原有客服功能不受影响，独立路由

## 图表设计方案

研报中嵌入以下 5 类可视化组件：

| 图表类型 | 数据来源 | 渲染方式 | 用途 |
|----------|----------|----------|------|
| **K 线图（蜡烛图）** | `get_stock_kline` 返回 OHLCV | ECharts candlestick | 展示股价走势与成交量 |
| **技术指标图** | K 线数据计算 MA/MACD/KDJ | ECharts line + bar overlay | 展示均线、MACD、KDJ 指标 |
| **资金流向图** | `get_money_flow` 返回主力/散户资金 | ECharts bar（正负柱） | 展示资金净流入/流出趋势 |
| **财务指标图** | `get_financial_data` 返回多期财务 | ECharts radar（雷达图）| 展示 PE/PB/ROE 等多维度对比 |
| **分析流程图** | LLM 在 prompt 指引下输出 Mermaid 代码 | Mermaid.js render | 展示分析逻辑、决策树 |

**数据流**：
```
Agent 调用工具 → 工具返回结构化数据 → SSE 推送 chart 事件
→ 前端收到 chart 事件 → 在报告对应位置插入 ECharts/Mermaid 实例
```

**LLM Mermaid 输出**：prompt 指引 LLM 在分析结论部分输出 ```mermaid 代码块，前端 Markdown 渲染器识别后调用 Mermaid 渲染。
