/**
 * 股票研报助手前端逻辑
 *
 * 功能：
 * 1. SSE 流式接收研报（session/tool_call/token/sources/done）
 * 2. 提示词模板管理（CRUD）
 * 3. 会话列表管理
 */

// —— 全局状态 ——
let srSessionId = null;
let srStreaming = false;
let srAbortController = null;

// —— 身份管理（与客服模块共用同一套认证） ——
const srToken = localStorage.getItem("token");
const srUserStr = localStorage.getItem("user");
let srUser = srUserStr ? JSON.parse(srUserStr) : null;
// 访客 id：未登录时用，存 localStorage 保持稳定
let srGuestId = localStorage.getItem("sr_guest_id");
if (!srToken && !srUser) {
    if (!srGuestId) {
        fetch("/api/auth/guest", { method: "POST" })
            .then(r => r.json())
            .then(d => {
                srGuestId = d.user_id;
                localStorage.setItem("sr_guest_id", srGuestId);
                srRenderUserBar();
                srLoadSessions();
            })
            .catch(() => {});
    }
}

function srCurrentUserId() { return srUser ? srUser.id : srGuestId; }
function srAuthHeaders() {
    const h = { "Content-Type": "application/json" };
    if (srToken) h["Authorization"] = "Bearer " + srToken;
    return h;
}

// —— Mermaid 初始化 ——
function srInitMermaid() {
    if (window.mermaid) {
        mermaid.initialize({
            startOnLoad: false,
            theme: 'default',
            flowchart: { curve: 'basis', padding: 15 },
            securityLevel: 'loose',
        });
    }
}

// 渲染页面上所有 .sr-mermaid-block 元素
async function srRenderMermaidBlocks(root) {
    if (!window.mermaid) return;
    const blocks = root ? root.querySelectorAll('.sr-mermaid-block') : document.querySelectorAll('.sr-mermaid-block');
    for (const block of blocks) {
        if (block.dataset.rendered) continue;
        const id = 'sr-mermaid-' + Math.random().toString(36).slice(2, 9);
        const code = block.textContent || block.dataset.code || '';
        try {
            const { svg } = await mermaid.render(id, code);
            block.innerHTML = svg;
            block.dataset.rendered = '1';
        } catch (e) {
            block.innerHTML = `<pre style="color:#dc2626;font-size:12px;">Mermaid 渲染失败：${srEscapeHtml(e.message)}</pre>`;
            block.dataset.rendered = '1';
        }
    }
}

// —— 初始化 ——
document.addEventListener('DOMContentLoaded', () => {
    srLoadPrompts();
    srInitInput();
    srRenderUserBar();
    srInitMermaid();
    if (srCurrentUserId()) srLoadSessions();
});

function srInitInput() {
    const input = document.getElementById('srInput');
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            srSend();
        }
    });
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
    // 顶部搜索栏 Enter 也触发发送
    document.getElementById('srStockInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const val = document.getElementById('srStockInput').value.trim();
            if (val) {
                document.getElementById('srInput').value = val;
                srSend();
            }
        }
    });
}

// —— 用户栏渲染 ——
function srRenderUserBar() {
    const badge = document.getElementById('srUserBadge');
    const authBtn = document.getElementById('srAuthBtn');
    if (srUser) {
        badge.textContent = `👤 ${srUser.display_name || srUser.username}`;
        authBtn.textContent = '退出';
        authBtn.href = '#';
        authBtn.onclick = (e) => {
            e.preventDefault();
            if (confirm('确认退出登录？')) {
                localStorage.removeItem('token');
                localStorage.removeItem('user');
                location.reload();
            }
        };
    } else {
        badge.textContent = srGuestId ? '访客模式' : '未登录';
        authBtn.textContent = '登录';
        authBtn.href = '/login?redirect=/stock_research';
        authBtn.onclick = null;
    }
}

// —— 提示词模板 ——
async function srLoadPrompts() {
    try {
        const resp = await fetch('/api/stock_research/prompts');
        const data = await resp.json();
        const sel = document.getElementById('srPromptSelect');
        sel.innerHTML = '<option value="">默认分析</option>';
        data.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = `[${t.category}] ${t.name}`;
            sel.appendChild(opt);
        });
    } catch (e) { /* ignore */ }
}

// —— 会话列表 ——
async function srLoadSessions() {
    if (!srCurrentUserId()) return;
    try {
        const params = new URLSearchParams();
        if (srUser) {
            // 登录用户，token 在 header 里
        } else {
            params.set('guest_id', srCurrentUserId());
        }
        const resp = await fetch(`/api/stock_research/reports?${params}`, { headers: srAuthHeaders() });
        const data = await resp.json();
        const list = document.getElementById('srSessionList');
        list.innerHTML = '';
        if (!data.sessions || data.sessions.length === 0) {
            list.innerHTML = '<div style="padding:12px;text-align:center;color:#64748b;font-size:12px;">暂无研报记录</div>';
            return;
        }
        data.sessions.forEach(s => {
            const div = document.createElement('div');
            div.className = 'sr-session-item';
            if (s.session_id === srSessionId) div.classList.add('active');
            div.innerHTML = `
                <span class="sr-session-title">${srEscapeHtml(s.title)}</span>
                <button class="sr-session-del" onclick="srDeleteSession('${s.session_id}', event)">×</button>
            `;
            div.onclick = () => srLoadHistory(s.session_id);
            list.appendChild(div);
        });
    } catch (e) { /* ignore */ }
}

async function srLoadHistory(sessionId) {
    try {
        const params = new URLSearchParams();
        if (!srUser) params.set('guest_id', srCurrentUserId());
        const resp = await fetch(`/api/customer_service/sessions/${sessionId}/history?${params}`, { headers: srAuthHeaders() });
        const data = await resp.json();
        srSessionId = sessionId;
        const area = document.getElementById('srReportArea');
        area.innerHTML = '';
        let lastAiMeta = null;
        if (data.messages && data.messages.length > 0) {
            data.messages.forEach(m => {
                if (m.role === 'user') {
                    srAppendUserMsg(m.content);
                } else {
                    srAppendAiMsg(m.content);
                    if (m.metadata) lastAiMeta = m.metadata;
                }
            });
        }
        // 仅当用户明确要求生成文档时 才恢复 PDF 下载按钮
        const lastAiMsg = area.querySelector('.sr-ai-message:last-of-type');
        if (lastAiMsg && lastAiMeta && lastAiMeta.pdf_requested) {
            const aiMsgData = { stock_name: lastAiMeta.stock_name, stock_code: lastAiMeta.stock_code };
            srEnsurePdfActionBar(lastAiMsg, aiMsgData);
        }
        srLoadSessions();
        area.scrollTop = area.scrollHeight;
    } catch (e) {
        console.error('Load history failed:', e);
    }
}

function srNewSession() {
    srSessionId = null;
    document.getElementById('srReportArea').innerHTML = `
        <div class="sr-welcome">
            <div class="sr-welcome-icon">📈</div>
            <div class="sr-welcome-title">股票研报助手</div>
            <div class="sr-welcome-desc">输入股票代码或名称，获取 AI 生成的专业研报</div>
            <div class="sr-welcome-examples">
                <button class="sr-example-btn" onclick="srQuickQuery('分析 000001 平安银行')">分析 000001</button>
                <button class="sr-example-btn" onclick="srQuickQuery('深度研报 600519 贵州茅台')">深度研报 600519</button>
                <button class="sr-example-btn" onclick="srQuickQuery('行业对比 000858 五粮液')">行业对比 000858</button>
            </div>
        </div>
    `;
    srLoadSessions();
}

function srQuickQuery(text) {
    document.getElementById('srInput').value = text;
    srSend();
}

async function srDeleteSession(sessionId, event) {
    event.stopPropagation();
    if (!confirm('确定删除此研报？')) return;
    try {
        const params = new URLSearchParams();
        if (!srUser) params.set('guest_id', srCurrentUserId());
        await fetch(`/api/customer_service/sessions/${sessionId}?${params}`, { method: 'DELETE', headers: srAuthHeaders() });
        if (srSessionId === sessionId) srNewSession();
        else srLoadSessions();
    } catch (e) { /* ignore */ }
}

// —— 发送消息 ——
async function srSend() {
    const input = document.getElementById('srInput');
    const message = input.value.trim();
    if (!message || srStreaming) return;

    const stockInput = document.getElementById('srStockInput');
    const stockCode = stockInput.value.trim();
    const fullMessage = stockCode ? `${stockCode} ${message}` : message;

    input.value = '';
    input.style.height = 'auto';
    stockInput.value = '';

    // 清除欢迎页
    const welcome = document.getElementById('srWelcome');
    if (welcome) welcome.remove();

    srAppendUserMsg(fullMessage);
    await srStreamReport(fullMessage);
}

async function srStreamReport(message) {
    srStreaming = true;
    srToggleStopButton(true);

    const promptId = document.getElementById('srPromptSelect').value;

    const aiMsg = srCreateAiMsg();

    srAbortController = new AbortController();

    try {
        const resp = await fetch('/api/stock_research/chat/stream', {
            method: 'POST',
            headers: srAuthHeaders(),
            body: JSON.stringify({
                message: message,
                prompt_id: promptId || null,
                session_id: srSessionId,
                guest_id: srUser ? null : srCurrentUserId(),
            }),
            signal: srAbortController.signal,
        });

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let tokenText = '';
        let traceItems = [];

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const ev = JSON.parse(line.slice(6));
                    const etype = ev.type;
                    const edata = ev.data;

                    if (etype === 'session') {
                        srSessionId = edata.session_id;
                        const mode = edata.mode || 'stock';
                        const header = aiMsg.querySelector('.sr-ai-header');
                        if (header) {
                            const label = mode === 'general' ? '万能助手' : '研报助手';
                            const icon = mode === 'general' ? '🌐' : 'AI';
                            header.innerHTML = `
                                <div class="sr-ai-avatar">${icon}</div>
                                <span>${label}</span>
                                <span class="sr-mode-badge ${mode === 'general' ? 'sr-mode-general' : 'sr-mode-stock'}">${mode === 'general' ? '万能模式' : '股票模式'}</span>
                            `;
                        }
                    } else if (etype === 'tool_call') {
                        traceItems.push(edata);
                        srUpdateTrace(aiMsg, traceItems);
                    } else if (etype === 'token') {
                        tokenText += edata;
                        srUpdateAiBody(aiMsg, tokenText);
                    } else if (etype === 'sources') {
                        srRenderSources(aiMsg, edata);
                    } else if (etype === 'pdf_ready') {
                        srRenderPdfReady(aiMsg, edata);
                    } else if (etype === 'done') {
                        srUpdateTrace(aiMsg, traceItems);
                        // PDF 下载按钮仅通过 pdf_ready 事件触发（后端检测到用户明确要求时才发送）
                        srLoadSessions();
                    } else if (etype === 'error') {
                        srUpdateAiBody(aiMsg, `⚠️ 错误：${edata}`);
                    }
                } catch (e) { /* skip */ }
            }
        }
    } catch (e) {
        if (e.name === 'AbortError') {
            // 用户主动中断，保留已生成内容
        } else {
            srUpdateAiBody(aiMsg, `⚠️ 网络错误：${e.message}`);
        }
    }

    srAbortController = null;
    srStreaming = false;
    srToggleStopButton(false);
}

function srStopStream() {
    if (srAbortController) srAbortController.abort();
}

function srToggleStopButton(streaming) {
    const btn = document.getElementById('srSendBtn');
    if (streaming) {
        btn.textContent = '停止';
        btn.onclick = srStopStream;
        btn.classList.add('sr-stop-btn');
        btn.disabled = false;
    } else {
        btn.textContent = '发送';
        btn.onclick = srSend;
        btn.classList.remove('sr-stop-btn');
        btn.disabled = false;
    }
}

// —— 演示模式：生成模拟股票研报文本 ——
async function srRunDemo() {
    const DEMO_CODE = '000001';
    const DEMO_NAME = '平安银行';
    const DEMO_MSG = `深度研报 ${DEMO_CODE} ${DEMO_NAME}（演示模式）`;

    const welcome = document.getElementById('srWelcome');
    if (welcome) welcome.remove();

    srAppendUserMsg(DEMO_MSG);
    srStreaming = true;
    document.getElementById('srSendBtn').disabled = true;

    const aiMsg = srCreateAiMsg();
    const traceItems = [];

    const toolCalls = [
        { name: 'get_stock_quote', args: { code: DEMO_CODE }, result: '获取实时行情' },
        { name: 'get_stock_kline', args: { code: DEMO_CODE, period: 'daily', count: 60 }, result: '获取K线数据' },
        { name: 'get_financial_data', args: { code: DEMO_CODE }, result: '获取财务指标' },
        { name: 'get_money_flow', args: { code: DEMO_CODE }, result: '获取资金流向' },
        { name: 'get_stock_news', args: { code: DEMO_CODE }, result: '获取最新新闻' },
    ];
    for (const tc of toolCalls) {
        traceItems.push(tc);
        srUpdateTrace(aiMsg, traceItems);
        await srDemoSleep(300);
    }

    const reportText = `## ${DEMO_NAME}（${DEMO_CODE}）深度研报

### 一、行情概述

${DEMO_NAME} 当前股价 **12.35 元**，涨跌幅 **+2.83%**，换手率 **1.85%**，市盈率 **5.62**，市净率 **0.48**，总市值约 **1200 亿元**。

### 二、技术面分析

从 K 线走势来看，${DEMO_NAME} 近期呈现 **震荡上行** 格局：
- MA5（12.10）> MA10（11.98）> MA20（11.75），短期均线多头排列
- MACD 红柱放大，DIF 上穿零轴，动能转强
- KDJ 的 J 值 85.3，处于超买区域，需关注短期回调风险

### 三、资金面分析

近 5 个交易日主力资金 **净流入 3.26 亿元**：
- 超大单净额 +1.82 亿，显示大资金积极建仓
- 大单净额 +0.95 亿，机构买入意愿强烈
- 散户资金小幅流出，筹码趋于集中

### 四、财务面分析

| 指标 | 本期 | 上期 | 变化 |
|------|------|------|------|
| ROE | 12.5% | 11.8% | ↑ 0.7pp |
| 营收增速 | 8.3% | 6.1% | ↑ 2.2pp |
| 净利润增速 | 15.2% | 12.4% | ↑ 2.8pp |
| 不良率 | 1.08% | 1.12% | ↓ 0.04pp |

### 五、风险提示

1. 宏观经济下行压力可能影响信贷需求
2. 同业竞争加剧，净息差收窄趋势未改
3. 房地产行业风险敞口需持续关注

### 六、投资建议

${DEMO_NAME} 作为城商行龙头，基本面稳健，估值处于历史低位（PB 0.48x），股息率超过 5%。技术面和资金面均呈现积极信号，建议 **逢低布局，中长期持有**。

> ⚠️ 以上分析基于演示数据，仅供参考，不构成投资建议。`;

    const tokens = reportText.split(/(?<=\n)|(?<=。)|(?<=：)|(?<=；)/);
    let tokenText = '';
    for (const token of tokens) {
        tokenText += token;
        srUpdateAiBody(aiMsg, tokenText);
        await srDemoSleep(15);
    }

    // —— done ——
    traceItems.push({ name: 'done', args: {}, result: '研报生成完成' });
    srUpdateTrace(aiMsg, traceItems);

    srStreaming = false;
    document.getElementById('srSendBtn').disabled = false;
}

function srDemoSleep(ms) {
    return new Promise(r => setTimeout(r, ms));
}

// —— 消息渲染 ——
function srAppendUserMsg(text) {
    const area = document.getElementById('srReportArea');
    const div = document.createElement('div');
    div.className = 'sr-message sr-msg-user';
    div.innerHTML = `<div class="sr-bubble-user">${srEscapeHtml(text)}</div>`;
    area.appendChild(div);
    area.scrollTop = area.scrollHeight;
}

function srCreateAiMsg() {
    const area = document.getElementById('srReportArea');
    const div = document.createElement('div');
    div.className = 'sr-message sr-msg-ai';
    div.innerHTML = `
        <div class="sr-ai-header">
            <div class="sr-ai-avatar">AI</div>
            <span>研报助手</span>
        </div>
        <div class="sr-thinking">
            <span>正在分析中</span>
            <div class="sr-dots"><div class="sr-dot"></div><div class="sr-dot"></div><div class="sr-dot"></div></div>
        </div>
        <div class="sr-ai-body" style="display:none;"></div>
        <div class="sr-trace" style="display:none;">
            <div class="sr-trace-title">🔧 工具调用</div>
            <div class="sr-trace-list"></div>
        </div>
    `;
    area.appendChild(div);
    area.scrollTop = area.scrollHeight;
    return div;
}

function srUpdateAiBody(aiMsg, text) {
    const thinking = aiMsg.querySelector('.sr-thinking');
    if (thinking) thinking.style.display = 'none';
    const body = aiMsg.querySelector('.sr-ai-body');
    body.style.display = 'block';

    let renderText = text;

    if (srStreaming) {
        // 补全未闭合的代码块
        const fenceCount = (text.match(/```/g) || []).length;
        if (fenceCount % 2 === 1) {
            renderText = text + '\n```';
        }
    }

    body.innerHTML = srRenderMarkdown(renderText);
    srRenderMermaidBlocks(body);
    const area = document.getElementById('srReportArea');
    if (area) area.scrollTop = area.scrollHeight;
}

function srUpdateTrace(aiMsg, items) {
    const trace = aiMsg.querySelector('.sr-trace');
    const list = aiMsg.querySelector('.sr-trace-list');
    if (items.length === 0) return;
    trace.style.display = 'block';
    const iconMap = {
        'search_stock_code': '🔍',
        'get_stock_quote': '📊',
        'get_stock_kline': '🕯️',
        'get_financial_data': '💰',
        'get_money_flow': '💸',
        'get_stock_news': '📰',
        'search_knowledge_base': '📚',
        'tavily_web_search': '🌐',
    };
    list.innerHTML = items.map(item => {
        const icon = iconMap[item.name] || '🔧';
        const args = JSON.stringify(item.args).slice(0, 60);
        return `<span class="sr-trace-item"><span class="sr-trace-icon">${icon}</span> ${item.name} <code style="font-size:11px;color:#94a3b8;">${args}</code></span>`;
    }).join('');
}

function srAppendAiMsg(text) {
    const aiMsg = srCreateAiMsg();
    srUpdateAiBody(aiMsg, text);
}

// —— Markdown 渲染（marked v4 API） ——
marked.setOptions({
    breaks: true,
    gfm: true,
    headerIds: false,
    mangle: false,
});

const srRenderer = new marked.Renderer();

srRenderer.code = function(code, lang) {
    if (lang === 'mermaid') {
        return `<div class="sr-mermaid-block">${srEscapeHtml(code)}</div>`;
    }
    const escaped = srEscapeHtml(code);
    return `<pre class="sr-md-code-block"><code class="language-${lang || ''}">${escaped}</code></pre>`;
};

srRenderer.codespan = function(text) {
    return `<code class="sr-md-inline-code">${srEscapeHtml(text)}</code>`;
};

srRenderer.link = function(href, title, text) {
    const titleAttr = title ? ` title="${srEscapeHtml(title)}"` : '';
    return `<a href="${srEscapeHtml(href)}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`;
};

srRenderer.list = function(body, ordered, start) {
    const type = ordered ? 'ol' : 'ul';
    const startAttr = ordered && start !== 1 ? ` start="${start}"` : '';
    return `<${type}${startAttr}>${body}</${type}>`;
};

function srRenderMarkdown(text) {
    if (!text) return '';
    try {
        // 预处理 1: 清理模型可能输出的脏 HTML 标签，保留表格标签
        let processed = text
            .replace(/<br\s*\/?\s*>/gi, '\n')
            .replace(/<\/?(div|span)[^>]*>/gi, '')
            .replace(/<\/?(p)\b[^>]*>/gi, '');

        // 预处理 2: 修复可能被 LLM 打乱的表格格式
        // 确保表格每行的 | 数量一致
        const lines = processed.split('\n');
        const fixedLines = [];
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];
            // 检测表格行
            if (line.trim().startsWith('|')) {
                // 收集连续的表格行
                const tableLines = [];
                while (i < lines.length && lines[i].trim().startsWith('|')) {
                    tableLines.push(lines[i]);
                    i++;
                }
                // 规范化：统一分隔行
                if (tableLines.length >= 2) {
                    const colCount = tableLines[0].split('|').length - 1;
                    // 修复分隔行
                    for (let t = 1; t < tableLines.length; t++) {
                        if (/^\s*\|[\s\-:]+\|\s*$/.test(tableLines[t])) {
                            const sep = '|' + Array(colCount).fill(' --- ').join('|') + '|';
                            tableLines[t] = sep;
                        }
                    }
                    // 修复数据行列数
                    for (let t = 0; t < tableLines.length; t++) {
                        if (!/^\s*\|[\s\-:]+\|\s*$/.test(tableLines[t])) {
                            const parts = tableLines[t].split('|');
                            while (parts.length - 1 < colCount) {
                                parts.splice(parts.length - 1, 0, ' ');
                            }
                            tableLines[t] = parts.join('|');
                        }
                    }
                }
                fixedLines.push(...tableLines);
            } else {
                fixedLines.push(line);
                i++;
            }
        }
        processed = fixedLines.join('\n');

        // 预处理 3: 将 [来源N] 转为 HTML 注释占位符（marked 不会修改 HTML 注释）
        const placeholderMap = {};
        processed = processed.replace(/\[来源([A-Za-z0-9]+)\]/g, (m, n) => {
            const key = `<!--CITE_${n}-->`;
            placeholderMap[key] = n;
            return key;
        });

        // 预处理 3.5: 移除模型手动输出的「参考来源」区块，避免和 sources 事件卡片重复
        processed = processed.replace(
            /\n*(#{1,6}\s*(参考来源|引用来源|资料来源|参考链接|来源链接)[^\n]*\n[\s\S]*)$/i,
            ''
        );

        // 预处理 4: 清理多余空行
        processed = processed.replace(/\n{3,}/g, '\n\n');

        let html = marked.parse(processed, { renderer: srRenderer });

        // 将裸 <table> 包一层 table-wrapper，保证溢出时横向可滚动
        html = html.replace(/<table>/g, '<div class="table-wrapper"><table>').replace(/<\/table>/g, '</table></div>');
        // 防止双重包裹（marked 已包一层也不会出错）
        html = html.replace(/<div class="table-wrapper">\s*<div class="table-wrapper">/g, '<div class="table-wrapper">').replace(/<\/table><\/div>\s*<\/table><\/div>/g, '</table></div>');

        // 还原脚注：把 <!--CITE_N--> 替换为可点击的脚注标签
        html = html.replace(/<!--CITE_([A-Za-z0-9]+)-->/g, (_, n) => {
            return `<sup class="sr-citation" title="来源${n}">${n}</sup>`;
        });

        // DOMPurify 安全过滤
        if (window.DOMPurify) {
            html = DOMPurify.sanitize(html, { ADD_TAGS: ['foreignObject'], ADD_ATTR: ['target'] });
        }

        return html;
    } catch (e) {
        console.error('Markdown render error:', e);
        return `<pre style="white-space:pre-wrap;color:#dc2626;">Markdown 渲染错误：${srEscapeHtml(e.message)}</pre>`;
    }
}

function srEscapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function srRenderSources(aiMsg, sources) {
    if (!sources || !sources.length) return;
    const body = aiMsg.querySelector('.sr-ai-body');
    if (!body) return;
    const existing = body.querySelector('.sr-sources-section');
    if (existing) existing.remove();
    const section = document.createElement('div');
    section.className = 'sr-sources-section';
    const title = document.createElement('div');
    title.className = 'sr-sources-title';
    title.textContent = '📎 参考来源';
    section.appendChild(title);
    const list = document.createElement('ol');
    list.className = 'sr-sources-list';
    for (const s of sources) {
        const li = document.createElement('li');
        const a = document.createElement('a');
        const url = s.source && s.source !== 'knowledge_base' ? s.source : '';
        if (url) {
            a.href = url;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.textContent = s.title || url;
        } else {
            a.textContent = s.title || '知识库文档';
            a.className = 'sr-source-kb';
        }
        li.appendChild(a);
        list.appendChild(li);
    }
    section.appendChild(list);
    body.appendChild(section);
}

// —— PDF 下载 / 导出 ——

function srRenderPdfReady(aiMsg, pdfData) {
    if (!aiMsg || !pdfData) return;
    const body = aiMsg.querySelector('.sr-ai-body');
    if (!body) return;
    // 先移除可能存在的旧 bar
    const old = body.querySelector('.sr-pdf-bar');
    if (old) old.remove();
    const bar = document.createElement('div');
    bar.className = 'sr-pdf-bar' + (pdfData.error ? ' error' : '');
    if (pdfData.error) {
        bar.innerHTML = `
            <div class="sr-pdf-info">
                <div class="sr-pdf-title">⚠️ PDF 生成失败</div>
                <div class="sr-pdf-meta">${srEscapeHtml(pdfData.error)}</div>
            </div>
            <button class="sr-pdf-btn secondary" onclick="srExportSessionPdf(this)">重试</button>
        `;
    } else {
        const name = pdfData.stock_name ? `${srEscapeHtml(pdfData.stock_name)}研报` : '研报';
        const code = pdfData.stock_code ? `· 代码 ${srEscapeHtml(pdfData.stock_code)}` : '';
        const size = pdfData.file_size_kb ? `（${pdfData.file_size_kb} KB）` : '';
        bar.innerHTML = `
            <div class="sr-pdf-info">
                <div class="sr-pdf-title">📄 ${name} PDF 已生成</div>
                <div class="sr-pdf-meta">点击右侧按钮下载 ${size} ${code}</div>
            </div>
            <a class="sr-pdf-btn" href="${srEscapeHtml(pdfData.download_url)}" target="_blank" rel="noopener noreferrer">⬇ 下载 PDF</a>
        `;
    }
    body.appendChild(bar);
}

function srEnsurePdfActionBar(aiMsg, autoPdf) {
    if (!aiMsg) return;
    const body = aiMsg.querySelector('.sr-ai-body');
    if (!body) return;
    // 如果 pdf_ready 已经渲染过 bar，就不再重复，只补一个「重新生成」
    const existing = body.querySelector('.sr-pdf-bar');
    if (existing) {
        // 给已有 bar 补一个「手动导出」按钮，方便重新生成
        if (!existing.querySelector('[data-action="re-export"]')) {
            const btn = document.createElement('button');
            btn.className = 'sr-pdf-btn secondary';
            btn.dataset.action = 're-export';
            btn.textContent = '🔄 重新生成';
            btn.onclick = function () { srExportSessionPdf(btn); };
            existing.appendChild(btn);
        }
        return;
    }
    const bar = document.createElement('div');
    bar.className = 'sr-pdf-bar';
    const name = autoPdf && autoPdf.stock_name ? `${srEscapeHtml(autoPdf.stock_name)}研报` : '当前研报';
    bar.innerHTML = `
        <div class="sr-pdf-info">
            <div class="sr-pdf-title">📄 导出为 PDF</div>
            <div class="sr-pdf-meta">将 ${name} 的完整内容、参考来源、免责声明生成精美 PDF 下载</div>
        </div>
        <button class="sr-pdf-btn" onclick="srExportSessionPdf(this)">📤 生成并下载 PDF</button>
    `;
    body.appendChild(bar);
}

async function srExportSessionPdf(btn) {
    if (!srSessionId) {
        alert('会话不存在，请先发送问题后再导出');
        return;
    }
    // 找所属 aiMsg，把现有 bar 变成 loading
    const aiMsg = btn && btn.closest && btn.closest('.sr-ai-message');
    const body = aiMsg && aiMsg.querySelector('.sr-ai-body');
    let bar = body && body.querySelector('.sr-pdf-bar');
    let barToRestoreHTML = null;
    if (bar) {
        barToRestoreHTML = bar.innerHTML;
        bar.className = 'sr-pdf-bar loading';
        bar.innerHTML = `
            <div class="sr-pdf-info">
                <div class="sr-pdf-title">⏳ 正在生成 PDF…</div>
                <div class="sr-pdf-meta">封面、页眉页脚、参考来源、免责声明排版中，请稍候</div>
            </div>
            <button class="sr-pdf-btn secondary" disabled>生成中…</button>
        `;
    }
    try {
        const resp = await fetch('/api/stock_research/generate_pdf', {
            method: 'POST',
            headers: Object.assign({}, srAuthHeaders(), { 'Content-Type': 'application/json' }),
            body: JSON.stringify({
                session_id: srSessionId,
                guest_id: srUser ? null : srCurrentUserId(),
            }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
            throw new Error(data.detail || data.error || '生成失败');
        }
        // 渲染成功 bar，附下载链接
        if (body) {
            const pdfData = {
                download_url: data.download_url,
                file_name: data.file_name,
                file_size_kb: data.file_size_kb,
                stock_code: data.stock_code,
                stock_name: data.stock_name,
            };
            // 移除当前 loading bar，用 srRenderPdfReady 重新画
            const loadingBar = body.querySelector('.sr-pdf-bar');
            if (loadingBar) loadingBar.remove();
            srRenderPdfReady(aiMsg, pdfData);
            // 补重新生成按钮
            srEnsurePdfActionBar(aiMsg, pdfData);
        } else {
            // 兜底：直接打开下载链接
            window.open(data.download_url, '_blank');
        }
    } catch (e) {
        if (bar && barToRestoreHTML !== null) {
            bar.className = 'sr-pdf-bar error';
            bar.innerHTML = `
                <div class="sr-pdf-info">
                    <div class="sr-pdf-title">⚠️ PDF 生成失败</div>
                    <div class="sr-pdf-meta">${srEscapeHtml(e.message || String(e))}</div>
                </div>
                <button class="sr-pdf-btn secondary" onclick="srExportSessionPdf(this)">重试</button>
            `;
        } else {
            alert('PDF 生成失败：' + (e.message || String(e)));
        }
    }
}

// —— 提示词管理弹窗 ——
async function srShowPromptModal() {
    document.getElementById('srPromptModal').classList.add('show');
    await srRenderPromptList();
}

function srHidePromptModal() {
    document.getElementById('srPromptModal').classList.remove('show');
}

async function srRenderPromptList() {
    const resp = await fetch('/api/stock_research/prompts');
    const data = await resp.json();
    const container = document.getElementById('srPromptList');
    container.innerHTML = '';

    // 新建模板区域
    const newCard = document.createElement('div');
    newCard.className = 'sr-prompt-card';
    newCard.innerHTML = `
        <div class="sr-prompt-card-header">
            <span class="sr-prompt-card-name">+ 新建模板</span>
        </div>
        <div class="sr-edit-area show" id="srNewPromptForm">
            <input type="text" id="srNewName" placeholder="模板名称" />
            <select id="srNewCategory">
                <option value="quick">快速分析</option>
                <option value="deep">深度研报</option>
                <option value="industry">行业对比</option>
                <option value="custom">自定义</option>
            </select>
            <textarea id="srNewContent" placeholder="模板内容，支持 {{stock_code}} {{stock_name}} 变量"></textarea>
            <button class="sr-btn-sm" onclick="srCreatePrompt()">保存</button>
        </div>
    `;
    container.appendChild(newCard);

    // 已有模板
    data.forEach(t => {
        const card = document.createElement('div');
        card.className = 'sr-prompt-card';
        card.innerHTML = `
            <div class="sr-prompt-card-header">
                <span class="sr-prompt-card-name">${srEscapeHtml(t.name)}</span>
                <span class="sr-prompt-cat-badge sr-cat-${t.category}">${t.category}</span>
            </div>
            <div class="sr-prompt-card-content">${srEscapeHtml(t.content)}</div>
            <div class="sr-prompt-actions">
                <button class="sr-btn-sm" onclick="srToggleEdit(this, '${t.id}')">编辑</button>
                <button class="sr-btn-sm danger" onclick="srDeletePrompt('${t.id}')">删除</button>
            </div>
            <div class="sr-edit-area" id="srEdit_${t.id}">
                <input type="text" id="srEditName_${t.id}" value="${srEscapeHtml(t.name)}" />
                <select id="srEditCat_${t.id}">
                    <option value="quick" ${t.category==='quick'?'selected':''}>快速分析</option>
                    <option value="deep" ${t.category==='deep'?'selected':''}>深度研报</option>
                    <option value="industry" ${t.category==='industry'?'selected':''}>行业对比</option>
                    <option value="custom" ${t.category==='custom'?'selected':''}>自定义</option>
                </select>
                <textarea id="srEditContent_${t.id}">${srEscapeHtml(t.content)}</textarea>
                <div style="display:flex;gap:8px;">
                    <button class="sr-btn-sm" onclick="srSavePrompt('${t.id}')">保存</button>
                    <button class="sr-btn-sm" onclick="srToggleEdit(this, '${t.id}')">取消</button>
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

function srToggleEdit(btn, id) {
    const editArea = document.getElementById(`srEdit_${id}`);
    editArea.classList.toggle('show');
}

async function srCreatePrompt() {
    const name = document.getElementById('srNewName').value.trim();
    const category = document.getElementById('srNewCategory').value;
    const content = document.getElementById('srNewContent').value.trim();
    if (!name || !content) return alert('请填写名称和内容');

    await fetch('/api/stock_research/prompts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, category, content }),
    });
    await srRenderPromptList();
    await srLoadPrompts();
}

async function srSavePrompt(id) {
    const name = document.getElementById(`srEditName_${id}`).value.trim();
    const category = document.getElementById(`srEditCat_${id}`).value;
    const content = document.getElementById(`srEditContent_${id}`).value.trim();
    if (!name || !content) return alert('请填写名称和内容');

    await fetch(`/api/stock_research/prompts/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, category, content }),
    });
    await srRenderPromptList();
    await srLoadPrompts();
}

async function srDeletePrompt(id) {
    if (!confirm('确定删除此模板？')) return;
    await fetch(`/api/stock_research/prompts/${id}`, { method: 'DELETE' });
    await srRenderPromptList();
    await srLoadPrompts();
}

// ==================== 知识库维护 / 检索 ====================
const SR_KB_API = '/api/knowledge';
const SR_KB_SEARCH_API = '/api/search';
const SR_KB_SUPPORTED = ['.txt', '.md', '.markdown', '.pdf'];
let srKbSelectedIds = new Set();
let srKbCurrentItems = [];
let srKbPendingFiles = [];
let srKbInited = false;

function srEscapeHtmlSafe(s) {
    return (s ?? '').toString()
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function srKbFmtDate(iso) {
    if (!iso) return '-';
    try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }); }
    catch { return iso; }
}

function srKbSetMsg(el, text, type = '') {
    el.textContent = text;
    el.className = 'sr-msg ' + type;
}

// —— Tab 切换 ——
function srSwitchTab(tab) {
    document.querySelectorAll('.sr-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    document.querySelectorAll('.sr-tab-panel').forEach(p => p.classList.remove('active'));
    const panelMap = { 'research': 'srTabResearch', 'kb-manage': 'srTabKbManage', 'kb-search': 'srTabKbSearch' };
    const panel = document.getElementById(panelMap[tab]);
    if (panel) panel.classList.add('active');
    if ((tab === 'kb-manage' || tab === 'kb-search') && !srKbInited) {
        srKbInit();
    }
    if (tab === 'kb-manage' && srKbInited) {
        srKbLoadList();
    }
}

// —— 知识库初始化 ——
function srKbInit() {
    srKbInited = true;
    srKbInitAddForm();
    srKbInitUpload();
    srKbInitList();
    srKbInitSearch();
    srKbLoadList();
}

// —— 新增知识 ——
function srKbInitAddForm() {
    const form = document.getElementById('srKbAddForm');
    const msg = document.getElementById('srKbFormMsg');
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const payload = {
            title: document.getElementById('srKbTitle').value.trim(),
            content: document.getElementById('srKbContent').value,
            source: document.getElementById('srKbSource').value.trim() || null,
        };
        srKbSetMsg(msg, '保存中...', '');
        try {
            const res = await fetch(SR_KB_API, {
                method: 'POST', headers: srAuthHeaders(), body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `保存失败 (${res.status})`);
            }
            srKbSetMsg(msg, '已添加到知识库', 'success');
            form.reset();
            await srKbLoadList();
        } catch (err) {
            srKbSetMsg(msg, err.message, 'error');
        }
    });
}

// —— 文件上传 ——
function srKbInitUpload() {
    const dropZone = document.getElementById('srKbDropZone');
    const fileInput = document.getElementById('srKbFileInput');
    const uploadForm = document.getElementById('srKbUploadForm');
    const uploadMsg = document.getElementById('srKbUploadMsg');
    const uploadBtn = document.getElementById('srKbUploadBtn');
    const clearBtn = document.getElementById('srKbClearFilesBtn');
    const fileListEl = document.getElementById('srKbFileList');

    function setUploadMsg(text, type = '') {
        uploadMsg.textContent = text;
        uploadMsg.className = 'sr-msg ' + type;
    }
    function fmtSize(b) {
        if (b < 1024) return `${b} B`;
        if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
        return `${(b / 1024 / 1024).toFixed(2)} MB`;
    }
    function hasSupportedExt(name) {
        const dot = name.slice(name.lastIndexOf('.')).toLowerCase();
        return SR_KB_SUPPORTED.includes(dot);
    }
    function renderFileList() {
        if (!srKbPendingFiles.length) { fileListEl.innerHTML = ''; return; }
        fileListEl.innerHTML = srKbPendingFiles.map((f, i) => `
            <div class="sr-file-item" data-idx="${i}">
                <span class="name">📄 ${srEscapeHtmlSafe(f.name)} <span class="size">${fmtSize(f.size)}</span></span>
                <span>
                    <span class="status" data-status-idx="${i}"></span>
                    <span class="remove" data-remove-idx="${i}" title="移除">✕</span>
                </span>
            </div>
        `).join('');
    }
    function addFiles(files) {
        let added = 0, skipped = 0;
        for (const f of files) {
            if (!hasSupportedExt(f.name)) { skipped++; continue; }
            srKbPendingFiles.push(f); added++;
        }
        renderFileList();
        if (skipped > 0) setUploadMsg(`已跳过 ${skipped} 个不支持的文件（仅支持 ${SR_KB_SUPPORTED.join(' / ')}）`, 'error');
        else if (added > 0) setUploadMsg(`已选择 ${added} 个文件，点击"开始上传"`, '');
    }

    dropZone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
    ['dragenter', 'dragover'].forEach(ev => {
        dropZone.addEventListener(ev, e => { e.preventDefault(); dropZone.classList.add('dragover'); });
    });
    ['dragleave', 'drop'].forEach(ev => {
        dropZone.addEventListener(ev, e => { e.preventDefault(); dropZone.classList.remove('dragover'); });
    });
    dropZone.addEventListener('drop', e => {
        const fs = e.dataTransfer?.files;
        if (fs && fs.length) addFiles(fs);
    });
    fileListEl.addEventListener('click', e => {
        const rm = e.target.closest('[data-remove-idx]');
        if (!rm) return;
        srKbPendingFiles.splice(parseInt(rm.dataset.removeIdx, 10), 1);
        renderFileList(); setUploadMsg('', '');
    });
    clearBtn.addEventListener('click', () => {
        srKbPendingFiles = []; renderFileList(); setUploadMsg('', '');
    });
    uploadForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!srKbPendingFiles.length) { setUploadMsg('请先选择文件', 'error'); return; }
        uploadBtn.disabled = true;
        setUploadMsg(`开始上传 ${srKbPendingFiles.length} 个文件...`, '');
        let totalChunks = 0, okCount = 0, failCount = 0;
        for (let i = 0; i < srKbPendingFiles.length; i++) {
            const f = srKbPendingFiles[i];
            const statusEl = fileListEl.querySelector(`[data-status-idx="${i}"]`);
            if (statusEl) { statusEl.textContent = '上传中...'; statusEl.className = 'status'; }
            const fd = new FormData(); fd.append('file', f, f.name);
            try {
                const res = await fetch(`${SR_KB_API}/upload`, { method: 'POST', body: fd, headers: srToken ? { 'Authorization': 'Bearer ' + srToken } : {} });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || `上传失败 (${res.status})`);
                }
                const data = await res.json();
                totalChunks += data.chunks || 0; okCount++;
                if (statusEl) { statusEl.textContent = `✓ ${data.chunks} 段`; statusEl.className = 'status success'; }
            } catch (err) {
                failCount++;
                if (statusEl) { statusEl.textContent = `✕ ${err.message}`; statusEl.className = 'status error'; }
            }
        }
        uploadBtn.disabled = false;
        const msgType = failCount > 0 ? 'error' : 'success';
        const msgText = failCount > 0
            ? `完成：成功 ${okCount} 个，失败 ${failCount} 个，共写入 ${totalChunks} 段`
            : `上传完成：${okCount} 个文件，共写入 ${totalChunks} 段知识`;
        setUploadMsg(msgText, msgType);
        if (okCount > 0) {
            srKbPendingFiles = srKbPendingFiles.filter((_, idx) => {
                const s = fileListEl.querySelector(`[data-status-idx="${idx}"]`);
                return s && !s.classList.contains('success');
            });
            renderFileList();
            await srKbLoadList();
        }
    });
}

// —— 列表加载 + 批量删除 + 编辑 ——
function srKbInitList() {
    const tbody = document.getElementById('srKbTbody');
    const selectAll = document.getElementById('srKbSelectAll');
    const batchDeleteBtn = document.getElementById('srKbBatchDeleteBtn');
    const batchClearBtn = document.getElementById('srKbBatchClearBtn');
    const keywordInput = document.getElementById('srKbKeyword');
    const refreshBtn = document.getElementById('srKbRefreshBtn');

    function updateBatchBar() {
        const count = srKbSelectedIds.size;
        document.getElementById('srKbBatchCount').textContent = `已选 ${count} 项`;
        batchDeleteBtn.disabled = count === 0;
        const visibleIds = srKbCurrentItems.map(it => it.id);
        const allChecked = visibleIds.length > 0 && visibleIds.every(id => srKbSelectedIds.has(id));
        selectAll.checked = allChecked;
        selectAll.indeterminate = !allChecked && count > 0;
    }

    tbody.addEventListener('change', e => {
        const cb = e.target.closest('.row-check');
        if (!cb) return;
        const id = cb.dataset.id;
        if (cb.checked) { srKbSelectedIds.add(id); cb.closest('tr').classList.add('selected'); }
        else { srKbSelectedIds.delete(id); cb.closest('tr').classList.remove('selected'); }
        updateBatchBar();
    });
    selectAll.addEventListener('change', () => {
        const visibleIds = srKbCurrentItems.map(it => it.id);
        if (selectAll.checked) {
            visibleIds.forEach(id => srKbSelectedIds.add(id));
            tbody.querySelectorAll('tr[data-row-id]').forEach(tr => { tr.classList.add('selected'); const cb = tr.querySelector('.row-check'); if (cb) cb.checked = true; });
        } else {
            visibleIds.forEach(id => srKbSelectedIds.delete(id));
            tbody.querySelectorAll('tr[data-row-id]').forEach(tr => { tr.classList.remove('selected'); const cb = tr.querySelector('.row-check'); if (cb) cb.checked = false; });
        }
        updateBatchBar();
    });
    batchClearBtn.addEventListener('click', () => {
        srKbSelectedIds.clear();
        tbody.querySelectorAll('tr[data-row-id]').forEach(tr => { tr.classList.remove('selected'); const cb = tr.querySelector('.row-check'); if (cb) cb.checked = false; });
        selectAll.checked = false; selectAll.indeterminate = false;
        updateBatchBar();
    });
    batchDeleteBtn.addEventListener('click', async () => {
        const ids = [...srKbSelectedIds];
        if (!ids.length) return;
        if (!confirm(`确定批量删除选中的 ${ids.length} 条知识？此操作不可恢复。`)) return;
        batchDeleteBtn.disabled = true;
        const orig = batchDeleteBtn.textContent;
        batchDeleteBtn.textContent = '删除中...';
        try {
            const res = await fetch(`${SR_KB_API}/batch_delete`, {
                method: 'POST', headers: srAuthHeaders(), body: JSON.stringify({ ids }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `批量删除失败 (${res.status})`);
            }
            const data = await res.json();
            data.deleted.forEach(id => srKbSelectedIds.delete(id));
            let msg = `已删除 ${data.deleted_count} 条`;
            if (data.not_found.length > 0) msg += `，${data.not_found.length} 条未找到已跳过`;
            await srKbLoadList();
        } catch (e) {
            alert(e.message);
        } finally {
            batchDeleteBtn.textContent = orig;
            updateBatchBar();
        }
    });
    tbody.addEventListener('click', async e => {
        const btn = e.target.closest('button[data-action]');
        if (!btn) return;
        const id = btn.dataset.id, action = btn.dataset.action;
        if (action === 'del') {
            if (!confirm('确定删除这条知识？')) return;
            try {
                const res = await fetch(`${SR_KB_API}/${encodeURIComponent(id)}`, { method: 'DELETE', headers: srToken ? { 'Authorization': 'Bearer ' + srToken } : {} });
                if (!res.ok && res.status !== 204) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || `删除失败 (${res.status})`);
                }
                await srKbLoadList();
            } catch (err) { alert(err.message); }
        } else if (action === 'edit') {
            try {
                const res = await fetch(`${SR_KB_API}/${encodeURIComponent(id)}`, { headers: srToken ? { 'Authorization': 'Bearer ' + srToken } : {} });
                if (!res.ok) throw new Error(`加载失败 (${res.status})`);
                const item = await res.json();
                document.getElementById('srKbEditId').value = item.id;
                document.getElementById('srKbEditTitle').value = item.title;
                document.getElementById('srKbEditSource').value = item.source || '';
                document.getElementById('srKbEditContent').value = item.content;
                srKbSetMsg(document.getElementById('srKbEditMsg'), '', '');
                document.getElementById('srKbEditModal').classList.add('show');
            } catch (e) { alert(e.message); }
        }
    });
    refreshBtn.addEventListener('click', srKbLoadList);
    keywordInput.addEventListener('input', () => {
        clearTimeout(window.__srKbT);
        window.__srKbT = setTimeout(srKbLoadList, 300);
    });
}

async function srKbLoadList() {
    const tbody = document.getElementById('srKbTbody');
    const emptyHint = document.getElementById('srKbEmptyHint');
    const keywordInput = document.getElementById('srKbKeyword');
    const formMsg = document.getElementById('srKbFormMsg');
    if (!tbody) return;
    const keyword = keywordInput.value.trim();
    const url = keyword ? `${SR_KB_API}?keyword=${encodeURIComponent(keyword)}&limit=200` : `${SR_KB_API}?limit=200`;
    tbody.innerHTML = '';
    try {
        const res = await fetch(url, { headers: srToken ? { 'Authorization': 'Bearer ' + srToken } : {} });
        if (!res.ok) throw new Error(`加载失败 (${res.status})`);
        const items = await res.json();
        srKbCurrentItems = items;
        const visibleIds = new Set(items.map(it => it.id));
        for (const id of [...srKbSelectedIds]) {
            if (!visibleIds.has(id)) srKbSelectedIds.delete(id);
        }
        if (!items.length) {
            emptyHint.classList.remove('hidden');
            return;
        }
        emptyHint.classList.add('hidden');
        tbody.innerHTML = items.map(it => {
            const checked = srKbSelectedIds.has(it.id) ? 'checked' : '';
            const cls = checked ? 'selected' : '';
            return `
            <tr class="${cls}" data-row-id="${srEscapeHtmlSafe(it.id)}">
                <td><input type="checkbox" class="row-check" data-id="${srEscapeHtmlSafe(it.id)}" ${checked} /></td>
                <td>${srEscapeHtmlSafe(it.title)}</td>
                <td class="content" title="${srEscapeHtmlSafe(it.content)}">${srEscapeHtmlSafe(it.content)}</td>
                <td>${srEscapeHtmlSafe(it.source || '-')}</td>
                <td>${srEscapeHtmlSafe(srKbFmtDate(it.created_at))}</td>
                <td>
                    <div class="row-actions">
                        <button data-action="edit" data-id="${srEscapeHtmlSafe(it.id)}">编辑</button>
                        <button class="danger" data-action="del" data-id="${srEscapeHtmlSafe(it.id)}">删除</button>
                    </div>
                </td>
            </tr>`;
        }).join('');
        // 更新批量栏状态
        const selectAll = document.getElementById('srKbSelectAll');
        const batchCountEl = document.getElementById('srKbBatchCount');
        const batchDeleteBtn = document.getElementById('srKbBatchDeleteBtn');
        const count = srKbSelectedIds.size;
        batchCountEl.textContent = `已选 ${count} 项`;
        batchDeleteBtn.disabled = count === 0;
        const visibleIdList = items.map(it => it.id);
        const allChecked = visibleIdList.length > 0 && visibleIdList.every(id => srKbSelectedIds.has(id));
        selectAll.checked = allChecked;
        selectAll.indeterminate = !allChecked && count > 0;
    } catch (e) {
        srKbSetMsg(formMsg, e.message, 'error');
    }
}

// —— 编辑弹窗 ——
function srKbHideEditModal() {
    document.getElementById('srKbEditModal').classList.remove('show');
}

function srKbInitEditForm() {
    const editForm = document.getElementById('srKbEditForm');
    const editMsg = document.getElementById('srKbEditMsg');
    editForm.addEventListener('submit', async e => {
        e.preventDefault();
        const id = document.getElementById('srKbEditId').value;
        const payload = {
            title: document.getElementById('srKbEditTitle').value.trim(),
            source: document.getElementById('srKbEditSource').value.trim() || null,
            content: document.getElementById('srKbEditContent').value,
        };
        srKbSetMsg(editMsg, '保存中...', '');
        try {
            const res = await fetch(`${SR_KB_API}/${encodeURIComponent(id)}`, {
                method: 'PUT', headers: srAuthHeaders(), body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `保存失败 (${res.status})`);
            }
            document.getElementById('srKbEditModal').classList.remove('show');
            await srKbLoadList();
        } catch (err) {
            srKbSetMsg(editMsg, err.message, 'error');
        }
    });
    document.getElementById('srKbCancelEdit').addEventListener('click', srKbHideEditModal);
}

// —— 知识库检索 ——
function srKbInitSearch() {
    const form = document.getElementById('srKbSearchForm');
    const rawBtn = document.getElementById('srKbRawBtn');
    const searchMsg = document.getElementById('srKbSearchMsg');
    const answerBox = document.getElementById('srKbAnswerBox');
    const sourcesBox = document.getElementById('srKbSourcesBox');
    const agentMode = document.getElementById('srKbAgentMode');

    function setMsg(text, type = '') {
        searchMsg.textContent = text;
        searchMsg.className = 'sr-msg ' + type;
    }
    function renderSources(items) {
        if (!items || !items.length) {
            sourcesBox.innerHTML = `<p class="sr-empty">未召回任何相关文档</p>`;
            return;
        }
        sourcesBox.innerHTML = items.map((it, i) => `
            <div class="sr-source-item">
                <div class="title">[来源${i + 1}] ${srEscapeHtmlSafe(it.title)}</div>
                <div class="meta">
                    来源：${srEscapeHtmlSafe(it.source || '-')}
                    ${it.score != null ? `<span class="sr-batch-count">  距离: ${it.score.toFixed(4)}</span>` : ''}
                </div>
                <div class="content">${srEscapeHtmlSafe(it.content)}</div>
            </div>
        `).join('');
    }

    async function doSearch(raw = false) {
        const query = document.getElementById('srKbQuery').value.trim();
        const k = parseInt(document.getElementById('srKbK').value, 10) || 4;
        if (!query) { setMsg('请输入问题', 'error'); return; }
        const useAgent = !raw && agentMode.checked;
        setMsg(useAgent ? 'Agent 思考中（可能多轮检索）...' : '检索中...', '');
        answerBox.textContent = '检索中，请稍候...';
        sourcesBox.innerHTML = '';
        try {
            let url = SR_KB_SEARCH_API;
            if (raw) url = `${SR_KB_SEARCH_API}/raw`;
            else if (useAgent) url = `${SR_KB_SEARCH_API}/agent`;
            const res = await fetch(url, {
                method: 'POST', headers: srAuthHeaders(), body: JSON.stringify({ query, k }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `检索失败 (${res.status})`);
            }
            const data = await res.json();
            if (raw) {
                answerBox.textContent = `共召回 ${data.length} 条相关文档（见下方）`;
                renderSources(data);
            } else {
                let html = srEscapeHtmlSafe(data.answer || '（模型未返回内容）');
                if (useAgent && data.tool_calls != null) {
                    html += `<div style="font-size:12px;color:#64748b;margin-top:8px;">Agent 模式 · 共调用检索工具 ${data.tool_calls} 次</div>`;
                }
                answerBox.innerHTML = html;
                renderSources(data.sources || []);
            }
            setMsg('完成', 'success');
        } catch (e) {
            setMsg(e.message, 'error');
            answerBox.textContent = '检索失败：' + e.message;
        }
    }

    form.addEventListener('submit', e => { e.preventDefault(); doSearch(false); });
    rawBtn.addEventListener('click', () => doSearch(true));
}

// 初始化编辑表单（在 DOMContentLoaded 后即可）
document.addEventListener('DOMContentLoaded', () => {
    srKbInitEditForm();
});
