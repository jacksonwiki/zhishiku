# Spec Delta: 股票研报助手

## ADDED Requirements

### Requirement: 股票实时行情获取
WHEN 用户输入股票代码并请求行情分析,
系统 SHALL 调用 AkShare API 获取该股票的实时行情数据（价格/涨跌幅/成交量/成交额）并返回给 LLM。

#### Scenario: 获取 A 股实时行情
GIVEN 用户输入股票代码 "000001"（平安银行）
WHEN agent 调用 get_stock_quote 工具
THEN 系统返回包含当前价格、涨跌幅、成交量、成交额的结构化数据
AND 数据在 60 秒缓存有效期内不重复请求 API

#### Scenario: 无效股票代码
GIVEN 用户输入股票代码 "999999"
WHEN agent 调用 get_stock_quote 工具
THEN 系统返回错误提示 "未找到该股票代码，请检查输入"
AND 不抛出异常，agent 可继续运行

### Requirement: K线与技术指标获取
WHEN 用户请求技术面分析,
系统 SHALL 调用 AkShare 获取指定周期的 K 线数据（OHLCV）并计算常用技术指标。

#### Scenario: 获取日K线数据
GIVEN 用户输入股票代码 "600519" 并请求日K线
WHEN agent 调用 get_stock_kline 工具，参数 period="daily", count=60
THEN 系统返回最近 60 个交易日的 OHLCV 数据
AND 计算并返回 MA5/MA10/MA20/MA60 均线值

#### Scenario: 获取周K线数据
GIVEN 用户输入股票代码并请求周K线
WHEN agent 调用 get_stock_kline 工具，参数 period="weekly"
THEN 系统返回周线 OHLCV 数据

### Requirement: 财务指标获取
WHEN 用户请求基本面分析,
系统 SHALL 调用 AkShare 获取该股票的财务指标数据。

#### Scenario: 获取财务指标
GIVEN 用户输入股票代码 "000858"（五粮液）
WHEN agent 调用 get_financial_data 工具
THEN 系统返回 PE/PB/ROE/营收增长率/净利润增长率等指标
AND 数据包含最新报告期日期

#### Scenario: 财务数据不可用
GIVEN 股票代码对应的公司暂无财务数据
WHEN agent 调用 get_financial_data 工具
THEN 系统返回 "暂无该股票的财务数据"
AND 不影响 agent 继续运行

### Requirement: 资金流向获取
WHEN 用户请求资金面分析,
系统 SHALL 调用 AkShare 获取主力资金流向数据。

#### Scenario: 获取资金流向
GIVEN 用户输入股票代码
WHEN agent 调用 get_money_flow 工具
THEN 系统返回最近 N 日的主力净流入/流出数据
AND 数据包含超大单/大单/中单/小单分类

### Requirement: 动态提示词模板管理
WHEN 管理员需要配置不同分析场景的提示词,
系统 SHALL 提供 CRUD 接口管理提示词模板，支持变量插值。

#### Scenario: 创建提示词模板
GIVEN 管理员提交模板名称 "快速分析"、分类 "quick"、内容含 {{stock_code}} 变量
WHEN 调用 POST /api/stock_research/prompts
THEN 系统存储模板并返回模板 ID
AND 模板可在后续对话中通过 prompt_id 引用

#### Scenario: 渲染模板
GIVEN 已存在模板 ID "tpl_001"，变量 stock_code="000001"
WHEN 调用 render_template("tpl_001", {"stock_code": "000001"})
THEN 系统将 {{stock_code}} 替换为 "000001" 并返回完整 prompt

#### Scenario: 预置默认模板
WHEN 系统首次启动
THEN 自动创建 3 个默认模板：快速分析 / 深度研报 / 行业对比
AND 已存在同名模板时不重复创建

### Requirement: 结构化研报生成
WHEN 用户请求生成股票研报,
系统 SHALL 使用动态提示词 + 股票数据工具 + LLM 生成结构化报告。

#### Scenario: 生成快速分析报告
GIVEN 用户输入股票代码 "000001" 并选择 "快速分析" 模板
WHEN 调用 POST /api/stock_research/chat/stream
THEN 系统流式输出包含行情摘要、技术面简评、操作建议的报告
AND 报告中引用的数据来源标注为 [来源N]

#### Scenario: 生成深度研报
GIVEN 用户输入股票代码并选择 "深度研报" 模板
WHEN 调用流式接口
THEN 系统依次调用行情/K线/财务/资金/新闻工具
AND 流式输出包含完整六大模块的报告（摘要/基本面/技术面/资金面/新闻舆情/预测建议）

#### Scenario: 工具调用失败降级
GIVEN 某个数据工具调用失败（如 AkShare 不可用）
WHEN agent 执行研报生成
THEN 系统在报告中标注 "XX数据获取失败，以下分析基于已有信息"
AND 不中断整体报告生成流程

### Requirement: 研报会话管理
WHEN 用户进行研报对话,
系统 SHALL 复用现有会话管理机制，并关联使用的提示词模板。

#### Scenario: 创建研报会话
GIVEN 用户登录并选择 "深度研报" 模板后发送消息
WHEN 系统创建会话
THEN 会话记录中关联 prompt_id 字段
AND 后续对话使用同一模板

#### Scenario: 切换提示词模板
GIVEN 用户在已有会话中切换分析模式
WHEN 用户选择新模板并发送消息
THEN 系统更新会话的 prompt_id
AND 使用新模板生成后续报告

### Requirement: 新闻舆情搜索
WHEN 用户请求新闻分析或研报需要新闻数据,
系统 SHALL 使用 Tavily 搜索获取该股票的最新新闻。

#### Scenario: 搜索个股新闻
GIVEN 用户输入股票代码 "600519"（贵州茅台）
WHEN agent 调用 get_stock_news 工具
THEN 系统使用 Tavily 搜索 "{股票名称} 最新消息"
AND 返回最近 5 条相关新闻（标题/链接/摘要）

#### Scenario: Tavily 未配置
GIVEN 未配置 TAVILY_API_KEY
WHEN agent 调用 get_stock_news 工具
THEN 系统返回 "新闻搜索功能未启用，请配置 Tavily API Key"
AND 不影响其他工具调用

### Requirement: 研报内嵌可视化图表
WHEN 工具返回结构化数据（K线/财务/资金流）且用户在前端查看研报,
系统 SHALL 通过 SSE 推送 chart 事件，前端使用 ECharts 渲染对应类型的图表。

#### Scenario: K 线蜡烛图渲染
GIVEN agent 调用 get_stock_kline 工具获取了 60 日 OHLCV 数据
WHEN SSE 推送 {"type": "chart", "data": {"chart_type": "kline", "code": "000001", "kline": [...], "volume": [...]}}
THEN 前端使用 ECharts candlestick 渲染 K 线主图
AND 在主图下方渲染成交量副图（bar 类型）
AND 图表插入到报告的"技术面分析"模块下方

#### Scenario: 技术指标叠加图渲染
GIVEN K 线数据已计算 MA5/MA10/MA20/MACD/KDJ 指标
WHEN SSE 推送 {"type": "chart", "data": {"chart_type": "indicators", "ma": {...}, "macd": {...}, "kdj": {...}}}
THEN 前端在 K 线主图上叠加 MA 均线（line 类型）
AND 在主图下方渲染 MACD 副图（DIF/DEA 线 + MACD 柱）
AND 再下方渲染 KDJ 副图（K/D/J 三线）

#### Scenario: 资金流向柱状图渲染
GIVEN agent 调用 get_money_flow 工具获取了资金流向数据
WHEN SSE 推送 {"type": "chart", "data": {"chart_type": "money_flow", "dates": [...], "main_flow": [...], "super_large": [...], "large": [...]}}
THEN 前端使用 ECharts bar 渲染资金净流入/流出柱状图
AND 正值显示为红色柱（流入），负值显示为绿色柱（流出）
AND 图表插入到报告的"资金面分析"模块下方

#### Scenario: 财务雷达图渲染
GIVEN agent 调用 get_financial_data 工具获取了多期财务指标
WHEN SSE 推送 {"type": "chart", "data": {"chart_type": "financial_radar", "indicators": ["PE", "PB", "ROE", "营收增速", "利润增速"], "current": [...], "previous": [...]}}
THEN 前端使用 ECharts radar 渲染雷达图
AND 当期数据与上期数据重叠对比
AND 图表插入到报告的"基本面分析"模块下方

#### Scenario: 图表数据不足时降级
GIVEN 某工具返回的数据不足以渲染图表（如 K 线数据少于 5 条）
WHEN SSE 推送 chart 事件
THEN 前端显示提示文字 "数据不足，无法生成图表"
AND 不影响研报文本内容的正常展示

### Requirement: 分析流程图渲染
WHEN LLM 在研报输出中包含 mermaid 代码块,
系统 SHALL 在前端使用 Mermaid.js 渲染为流程图/决策树。

#### Scenario: 渲染分析决策流程图
GIVEN LLM 输出中包含 ```mermaid graph TD; A[行情分析] --> B{趋势判断}; B -->|上涨| C[买入建议]; B -->|下跌| D[卖出建议]; ```
WHEN 前端 Markdown 渲染器遇到 mermaid 代码块
THEN 系统调用 mermaid.render() 将代码转换为 SVG 流程图
AND 流程图插入到报告的"预测与建议"模块下方
AND SVG 自适应宽度，支持点击放大

#### Scenario: 渲染资金流向逻辑图
GIVEN LLM 输出中包含 ```mermaid flowchart LR; 主力流入 --> 价格上涨; 散户流出 --> 价格波动; ```
WHEN 前端识别 mermaid 代码块
THEN 系统渲染为左右流向图
AND 图表样式与报告主题配色一致

#### Scenario: 无效 mermaid 代码降级
GIVEN LLM 输出的 mermaid 代码有语法错误
WHEN mermaid.render() 解析失败
THEN 前端显示原始 mermaid 代码文本（代码块形式）
AND 在代码上方显示提示 "流程图渲染失败，请检查代码语法"

## MODIFIED Requirements

### Requirement: 会话存储扩展
WHEN 系统创建研报会话,
sessions 表 SHALL 新增 prompt_id 字段以关联提示词模板。

#### Scenario: 创建带提示词的会话
GIVEN 用户选择 "深度研报" 模板（ID: tpl_002）后创建会话
WHEN 系统插入 sessions 表记录
THEN prompt_id 字段值为 "tpl_002"

#### Scenario: 兼容旧会话
GIVEN sessions 表已存在但无 prompt_id 列
WHEN 系统启动
THEN 自动 ALTER TABLE 添加 prompt_id 列，默认值为 NULL
AND 原有客服会话不受影响
