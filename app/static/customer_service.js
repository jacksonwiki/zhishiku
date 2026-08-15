// 智能客服页面交互：用户系统 + 会话列表 + 流式输出
const API_STREAM = "/api/customer_service/chat/stream";
const API_SESSIONS = "/api/customer_service/sessions";

const messagesEl = document.getElementById("messages");
const chatForm = document.getElementById("chatForm");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("sendBtn");
const sessionListEl = document.getElementById("sessionList");
const newChatBtn = document.getElementById("newChatBtn");
const chatTitleEl = document.getElementById("chatTitle");
const renameBtn = document.getElementById("renameBtn");
const userNameEl = document.getElementById("userName");
const authBtn = document.getElementById("authBtn");

// —— 身份管理 ——
const token = localStorage.getItem("token");
const userStr = localStorage.getItem("user");
let user = userStr ? JSON.parse(userStr) : null;
// 访客 id：未登录时用，存 localStorage 保持稳定
let guestId = localStorage.getItem("guest_id");
if (!token && !user) {
    if (!guestId) {
        // 调后端生成稳定访客 id
        fetch("/api/auth/guest", { method: "POST" })
            .then(r => r.json())
            .then(d => {
                guestId = d.user_id;
                localStorage.setItem("guest_id", guestId);
                renderUserBar();
                loadSessions();
            })
            .catch(() => {});
    }
}

// 当前用户 id（用于请求）
function currentUserId() { return user ? user.id : guestId; }
function authHeaders() {
    const h = { "Content-Type": "application/json" };
    if (token) h["Authorization"] = "Bearer " + token;
    return h;
}

// —— 会话状态 ——
let sessionId = null;
let sessions = [];
let loadingHistory = false;

// —— 顶部用户栏渲染 ——
function renderUserBar() {
    if (user) {
        userNameEl.textContent = user.display_name || user.username;
        userNameEl.title = `用户：${user.username}`;
        authBtn.textContent = "退出";
        authBtn.href = "#";
        authBtn.onclick = (e) => {
            e.preventDefault();
            if (confirm("确认退出登录？")) {
                localStorage.removeItem("token");
                localStorage.removeItem("user");
                location.reload();
            }
        };
    } else {
        userNameEl.textContent = guestId ? "访客" : "未登录";
        authBtn.textContent = "登录";
        authBtn.href = "/login?redirect=/customer_service";
        authBtn.onclick = null;
    }
}

// —— 会话列表 ——
async function loadSessions() {
    if (!currentUserId()) return;
    try {
        const params = user ? "" : `?guest_id=${encodeURIComponent(guestId)}`;
        const res = await fetch(`${API_SESSIONS}${params}`, { headers: authHeaders() });
        if (!res.ok) throw new Error("加载会话列表失败");
        const data = await res.json();
        sessions = data.sessions || [];
        renderSessionList();
    } catch (e) {
        console.error(e);
    }
}

function renderSessionList() {
    if (!sessions.length) {
        sessionListEl.innerHTML = '<div class="empty-sessions">暂无历史会话</div>';
        return;
    }
    sessionListEl.innerHTML = sessions.map(s => {
        const time = formatTime(s.updated_at);
        const active = s.session_id === sessionId ? "active" : "";
        const timeHtml = time ? `<div class="meta">${time}</div>` : "";
        return `<div class="session-item ${active}" data-id="${s.session_id}" onclick="loadSession('${s.session_id}')">
            <div style="flex:1;overflow:hidden">
                <div class="title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
                ${timeHtml}
            </div>
            <span class="del" onclick="deleteSession(event, '${s.session_id}')" title="删除">×</span>
        </div>`;
    }).join("");
}

function formatTime(ts) {
    if (!ts || ts <= 0) return "";
    const d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return "";
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + "分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
    return `${d.getMonth() + 1}/${d.getDate()}`;
}

async function loadSession(sid) {
    if (loadingHistory) return;
    loadingHistory = true;
    try {
        const params = user ? "" : `?guest_id=${encodeURIComponent(guestId)}`;
        const res = await fetch(`${API_SESSIONS}/${sid}/history${params}`, { headers: authHeaders() });
        if (!res.ok) throw new Error("加载历史失败");
        const data = await res.json();
        sessionId = sid;
        chatTitleEl.textContent = data.title || "智能客服";
        renameBtn.style.display = "";
        // 渲染历史消息
        messagesEl.innerHTML = "";
        if (!data.messages.length) {
            messagesEl.innerHTML = '<div class="msg-row assistant"><div class="avatar assistant">AI</div><div class="bubble">你好，我是智能客服助手 👋 有什么可以帮您？</div></div>';
        } else {
            data.messages.forEach(m => appendMessage(m.role, m.content));
        }
        renderSessionList();
    } catch (e) {
        appendMessage("assistant", "加载历史失败：" + e.message);
    } finally {
        loadingHistory = false;
    }
}

async function deleteSession(e, sid) {
    e.stopPropagation();
    if (!confirm("确认删除该会话？")) return;
    try {
        const params = user ? "" : `?guest_id=${encodeURIComponent(guestId)}`;
        const res = await fetch(`${API_SESSIONS}/${sid}${params}`, {
            method: "DELETE",
            headers: authHeaders(),
        });
        if (!res.ok) throw new Error("删除失败");
        if (sid === sessionId) {
            startNewChat();
        }
        await loadSessions();
    } catch (e) {
        alert("删除失败：" + e.message);
    }
}

// —— 重命名 ——
renameBtn.onclick = async () => {
    if (!sessionId) return;
    const title = prompt("请输入新标题", chatTitleEl.textContent);
    if (!title) return;
    try {
        const params = user ? "" : `?guest_id=${encodeURIComponent(guestId)}`;
        const res = await fetch(`${API_SESSIONS}/${sessionId}/title${params}`, {
            method: "PUT",
            headers: authHeaders(),
            body: JSON.stringify({ title }),
        });
        if (!res.ok) throw new Error("重命名失败");
        chatTitleEl.textContent = title;
        await loadSessions();
    } catch (e) {
        alert(e.message);
    }
};

// —— 新对话 ——
function startNewChat() {
    sessionId = null;
    chatTitleEl.textContent = "智能客服";
    renameBtn.style.display = "none";
    messagesEl.innerHTML = '<div class="msg-row assistant"><div class="avatar assistant">AI</div><div class="bubble">你好，我是智能客服助手 👋 有什么可以帮您？我可以查询内部知识库，也可以搜索网络获取最新信息。</div></div>';
    renderSessionList();
}
newChatBtn.onclick = startNewChat;

// —— 聊天逻辑（流式）——
function escapeHtml(s) {
    return (s ?? "").toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

/**
 * 轻量 Markdown 渲染器，支持：
 *   - **粗体** / *斜体* / `行内代码`
 *   - 有序 / 无序列表
 *   - [链接](url)
 *   - [来源N] 引用脚注
 *   - 换行 / 段落
 * 策略：先转义 HTML，再对转义后的文本做正则替换，避免 XSS。
 */
function renderMarkdown(text) {
    if (!text) return "";
    let s = escapeHtml(text);

    // 行内代码 `code`
    s = s.replace(/`([^`]+?)`/g, '<code class="md-code">$1</code>');

    // 粗体 **text**
    s = s.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>');

    // 斜体 *text* (注意：不匹配 ** 内的 *，已被上面处理过)
    s = s.replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>');

    // [来源N] 引用脚注 — 支持 [来源1] [来源2] [来源N] [来源A] 等
    s = s.replace(/\[来源([A-Za-z0-9]+)\]/g, '<sup class="cite" data-ref="$1" title="查看来源 $1">[$1]</sup>');

    // [链接文本](url)
    s = s.replace(/\[([^\]]+?)\]\((https?:[^)\s]+?)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer" class="md-link">$1</a>');

    // 处理列表：先按行拆分，识别列表行，再合并
    const lines = s.split("\n");
    const processed = [];
    let inUl = false, inOl = false;

    function closeLists() {
        if (inUl) { processed.push("</ul>"); inUl = false; }
        if (inOl) { processed.push("</ol>"); inOl = false; }
    }

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        // 无序列表: * 或 - 开头
        const ulMatch = line.match(/^([ \t]*?)[*\-]\s+(.+)/);
        // 有序列表: 1. 开头
        const olMatch = line.match(/^([ \t]*?)\d+\.\s+(.+)/);

        if (ulMatch) {
            closeLists();
            if (!inUl) { processed.push('<ul class="md-list">'); inUl = true; }
            processed.push(`<li>${ulMatch[2]}</li>`);
        } else if (olMatch) {
            closeLists();
            if (!inOl) { processed.push('<ol class="md-list">'); inOl = true; }
            processed.push(`<li>${olMatch[2]}</li>`);
        } else if (line.trim() === "") {
            closeLists();
            processed.push("");
        } else {
            closeLists();
            processed.push(line);
        }
    }
    closeLists();

    s = processed.join("\n");

    // 换行：非列表已处理，剩余换行转 <br>
    s = s.replace(/\n/g, "<br>");

    return s;
}

function appendMessage(role, text) {
    const row = document.createElement("div");
    row.className = `msg-row ${role}`;
    const avatar = document.createElement("div");
    avatar.className = `avatar ${role}`;
    avatar.textContent = role === "user" ? "我" : "AI";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = renderMarkdown(text);
    row.appendChild(avatar);
    row.appendChild(bubble);
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
}

function appendThinking() {
    const row = document.createElement("div");
    row.className = "msg-row assistant thinking-row";
    const avatar = document.createElement("div");
    avatar.className = "avatar assistant";
    avatar.textContent = "AI";
    const bubble = document.createElement("div");
    bubble.className = "bubble thinking";
    bubble.innerHTML = '<span class="thinking-dots"><span></span><span></span><span></span></span> 思考中';
    row.appendChild(avatar);
    row.appendChild(bubble);
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return row;
}

function appendAgentTrace(toolCalls) {
    if (!toolCalls.length) return;
    const row = document.createElement("div");
    row.className = "msg-row assistant";
    const avatar = document.createElement("div");
    avatar.className = "avatar assistant";
    avatar.textContent = "AI";
    const trace = document.createElement("div");
    trace.className = "agent-trace";
    let html = `<div class="trace-title"><span class="trace-icon">🔧</span> 调用了 ${toolCalls.length} 次工具</div><ul class='trace-list'>`;
    toolCalls.forEach((tc) => {
        const argsStr = escapeHtml(JSON.stringify(tc.args));
        const icon = tc.name === "tavily_web_search" ? "🌐" : "📖";
        html += `<li>
            <span class="trace-tool">${icon} ${escapeHtml(tc.name)}</span>
            <span class="trace-args" title="${argsStr}">${argsStr.length > 40 ? argsStr.slice(0, 40) + '…' : argsStr}</span>
            <span class="trace-meta">${tc.sources_count} 条结果</span>
        </li>`;
    });
    html += "</ul>";
    trace.innerHTML = html;
    row.appendChild(avatar);
    row.appendChild(trace);
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendSources(sources) {
    if (!sources.length) return;
    if (!answerBubble) return;

    const row = answerBubble.parentElement;
    if (!row) return;

    let html = `<div class="answer-sources">`;
    html += `<div class="answer-sources-title">📎 参考来源 (${sources.length})</div>`;
    html += `<div class="answer-sources-list">`;
    sources.forEach((s, i) => {
        const url = s.source || s.url || "";
        const title = escapeHtml(s.title);
        const content = escapeHtml(s.content || "").replace(/\s+/g, " ").slice(0, 120);
        const linkHtml = url
            ? `<a class="as-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="查看原文">🔗 ${title}</a>`
            : `<span class="as-title">${title}</span>`;
        html += `<div class="as-item" data-ref="${i + 1}">
            <span class="as-badge">${i + 1}</span>
            <div class="as-body">
                ${linkHtml}
                ${content ? `<div class="as-content">${content}${s.content && s.content.length > 120 ? "…" : ""}</div>` : ""}
            </div>
        </div>`;
    });
    html += `</div></div>`;

    const wrapper = document.createElement("div");
    wrapper.className = "answer-sources-wrap";
    wrapper.innerHTML = html;
    row.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function send() {
    const message = inputEl.value.trim();
    if (!message) return;
    inputEl.value = "";
    sendBtn.disabled = true;
    appendMessage("user", message);

    const thinkingRow = appendThinking();
    const toolCalls = [];
    let answerBubble = null;
    let answerText = "";

    try {
        const body = { message, k: 4 };
        if (sessionId) body.session_id = sessionId;
        if (!user && guestId) body.guest_id = guestId;
        const res = await fetch(API_STREAM, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `请求失败 (${res.status})`);
        }
        thinkingRow.remove();

        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf("\n\n")) >= 0) {
                const raw = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 2);
                const line = raw.replace(/^data:\s*/, "").trim();
                if (!line) continue;
                let ev;
                try { ev = JSON.parse(line); } catch { continue; }
                const { type, data } = ev;
                if (type === "session") {
                    sessionId = data.session_id;
                    renameBtn.style.display = "";
                } else if (type === "tool_call") {
                    toolCalls.push(data);
                    appendAgentTrace([data]);
                } else if (type === "token") {
                    if (!answerBubble) {
                        answerBubble = appendMessage("assistant", "");
                        answerText = "";
                    }
                    answerText += data;
                    answerBubble.innerHTML = renderMarkdown(answerText);
                    messagesEl.scrollTop = messagesEl.scrollHeight;
                } else if (type === "sources") {
                    appendSources(data || []);
                } else if (type === "done") {
                    if (!answerBubble) {
                        appendMessage("assistant", "（未返回内容）");
                    }
                    // 刷新会话列表（新会话会出现在列表中）
                    loadSessions();
                } else if (type === "error") {
                    appendMessage("assistant", "出错了：" + data);
                }
            }
        }
    } catch (e) {
        thinkingRow.remove();
        appendMessage("assistant", "出错了：" + e.message);
    } finally {
        sendBtn.disabled = false;
        inputEl.focus();
    }
}

chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    send();
});

inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
    }
});

// —— 初始化 ——
renderUserBar();
loadSessions();
