// 检索问答页面交互
const API = "/api/search";

const form = document.getElementById("searchForm");
const searchMsg = document.getElementById("searchMsg");
const answerBox = document.getElementById("answerBox");
const sourcesBox = document.getElementById("sourcesBox");
const rawBtn = document.getElementById("rawBtn");
const agentMode = document.getElementById("agentMode");
const searchBtn = document.getElementById("searchBtn");

function setMsg(text, type = "") {
    searchMsg.textContent = text;
    searchMsg.className = "msg " + type;
}

function escapeHtml(s) {
    return (s ?? "").toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function renderSources(items) {
    if (!items.length) {
        sourcesBox.innerHTML = `<p class="empty">未召回任何相关文档</p>`;
        return;
    }
    sourcesBox.innerHTML = items.map((it, i) => `
        <div class="source-item">
            <div class="title">[来源${i + 1}] ${escapeHtml(it.title)}</div>
            <div class="meta">
                来源：${escapeHtml(it.source || "-")}
                ${it.score != null ? `<span class="score">  距离: ${it.score.toFixed(4)}</span>` : ""}
            </div>
            <div class="content">${escapeHtml(it.content)}</div>
        </div>
    `).join("");
}

async function doSearch(raw = false) {
    const query = document.getElementById("query").value.trim();
    const k = parseInt(document.getElementById("k").value, 10) || 4;
    if (!query) {
        setMsg("请输入问题", "error");
        return;
    }
    const useAgent = !raw && agentMode.checked;
    setMsg(useAgent ? "Agent 思考中（可能多轮检索）..." : "检索中...", "");
    answerBox.textContent = "检索中，请稍候...";
    sourcesBox.innerHTML = "";
    try {
        let url;
        if (raw) {
            url = `${API}/raw`;
        } else if (useAgent) {
            url = `${API}/agent`;
        } else {
            url = API;
        }
        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, k }),
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
            let html = escapeHtml(data.answer || "（模型未返回内容）");
            if (useAgent && data.tool_calls != null) {
                html += `<div class="answer-meta">Agent 模式 · 共调用检索工具 ${data.tool_calls} 次</div>`;
            }
            answerBox.innerHTML = html;
            renderSources(data.sources || []);
        }
        setMsg("完成", "success");
    } catch (e) {
        setMsg(e.message, "error");
        answerBox.textContent = "检索失败：" + e.message;
    }
}

form.addEventListener("submit", (e) => {
    e.preventDefault();
    doSearch(false);
});

rawBtn.addEventListener("click", () => doSearch(true));
