// 知识库维护页面交互
const API = "/api/knowledge";

const form = document.getElementById("addForm");
const formMsg = document.getElementById("formMsg");
const tbody = document.getElementById("tbody");
const emptyHint = document.getElementById("emptyHint");
const keywordInput = document.getElementById("keyword");
const refreshBtn = document.getElementById("refreshBtn");

const modal = document.getElementById("modal");
const editForm = document.getElementById("editForm");
const editMsg = document.getElementById("editMsg");
const cancelEdit = document.getElementById("cancelEdit");

// 批量删除相关
const selectAll = document.getElementById("selectAll");
const batchDeleteBtn = document.getElementById("batchDeleteBtn");
const batchClearBtn = document.getElementById("batchClearBtn");
const batchCountEl = document.getElementById("batchCount");
const selectedIds = new Set(); // 当前选中的 ID 集合
let currentItems = []; // 当前渲染的数据，便于全选时取 ID

function setMsg(el, text, type = "") {
    el.textContent = text;
    el.className = "msg " + type;
}

function fmtDate(iso) {
    if (!iso) return "-";
    try {
        const d = new Date(iso);
        return d.toLocaleString("zh-CN", { hour12: false });
    } catch {
        return iso;
    }
}

function escapeHtml(s) {
    return (s ?? "").toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

async function loadList() {
    const keyword = keywordInput.value.trim();
    const url = keyword ? `${API}?keyword=${encodeURIComponent(keyword)}&limit=200` : `${API}?limit=200`;
    tbody.innerHTML = "";
    setMsg(formMsg, "", "");
    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`加载失败 (${res.status})`);
        const items = await res.json();
        currentItems = items;
        // 清理已不存在的选中 ID
        const visibleIds = new Set(items.map((it) => it.id));
        for (const id of [...selectedIds]) {
            if (!visibleIds.has(id)) selectedIds.delete(id);
        }
        if (!items.length) {
            emptyHint.classList.remove("hidden");
            updateBatchBar();
            return;
        }
        emptyHint.classList.add("hidden");
        tbody.innerHTML = items.map((it) => {
            const checked = selectedIds.has(it.id) ? "checked" : "";
            const selectedCls = checked ? "selected" : "row-hover";
            return `
            <tr class="${selectedCls}" data-row-id="${escapeHtml(it.id)}">
                <td><input type="checkbox" class="row-check" data-id="${escapeHtml(it.id)}" ${checked} /></td>
                <td>${escapeHtml(it.title)}</td>
                <td class="content">${escapeHtml(it.content)}</td>
                <td>${escapeHtml(it.source || "-")}</td>
                <td>${escapeHtml(fmtDate(it.created_at))}</td>
                <td>
                    <div class="row-actions">
                        <button class="btn" data-action="edit" data-id="${escapeHtml(it.id)}">编辑</button>
                        <button class="btn danger" data-action="del" data-id="${escapeHtml(it.id)}">删除</button>
                    </div>
                </td>
            </tr>
            `;
        }).join("");
        updateBatchBar();
    } catch (e) {
        setMsg(formMsg, e.message, "error");
    }
}

function updateBatchBar() {
    const count = selectedIds.size;
    batchCountEl.textContent = `已选 ${count} 项`;
    batchDeleteBtn.disabled = count === 0;
    // 全选复选框状态：当前可见行是否全选
    const visibleIds = currentItems.map((it) => it.id);
    const allChecked = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));
    selectAll.checked = allChecked;
    selectAll.indeterminate = !allChecked && count > 0;
}

// 行复选框切换
tbody.addEventListener("change", (e) => {
    const cb = e.target.closest(".row-check");
    if (!cb) return;
    const id = cb.dataset.id;
    if (cb.checked) {
        selectedIds.add(id);
        cb.closest("tr").classList.add("selected");
        cb.closest("tr").classList.remove("row-hover");
    } else {
        selectedIds.delete(id);
        cb.closest("tr").classList.remove("selected");
        cb.closest("tr").classList.add("row-hover");
    }
    updateBatchBar();
});

// 全选 / 取消全选
selectAll.addEventListener("change", () => {
    const visibleIds = currentItems.map((it) => it.id);
    if (selectAll.checked) {
        visibleIds.forEach((id) => selectedIds.add(id));
        tbody.querySelectorAll("tr[data-row-id]").forEach((tr) => {
            tr.classList.add("selected");
            tr.classList.remove("row-hover");
            const cb = tr.querySelector(".row-check");
            if (cb) cb.checked = true;
        });
    } else {
        visibleIds.forEach((id) => selectedIds.delete(id));
        tbody.querySelectorAll("tr[data-row-id]").forEach((tr) => {
            tr.classList.remove("selected");
            tr.classList.add("row-hover");
            const cb = tr.querySelector(".row-check");
            if (cb) cb.checked = false;
        });
    }
    updateBatchBar();
});

// 取消选择
batchClearBtn.addEventListener("click", () => {
    selectedIds.clear();
    tbody.querySelectorAll("tr[data-row-id]").forEach((tr) => {
        tr.classList.remove("selected");
        tr.classList.add("row-hover");
        const cb = tr.querySelector(".row-check");
        if (cb) cb.checked = false;
    });
    selectAll.checked = false;
    selectAll.indeterminate = false;
    updateBatchBar();
});

// 批量删除
batchDeleteBtn.addEventListener("click", async () => {
    const ids = [...selectedIds];
    if (!ids.length) return;
    if (!confirm(`确定批量删除选中的 ${ids.length} 条知识？此操作不可恢复。`)) return;
    batchDeleteBtn.disabled = true;
    const originalText = batchDeleteBtn.textContent;
    batchDeleteBtn.textContent = "删除中...";
    try {
        const res = await fetch(`${API}/batch_delete`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `批量删除失败 (${res.status})`);
        }
        const data = await res.json();
        // 删除成功的从选中集合移除
        data.deleted.forEach((id) => selectedIds.delete(id));
        let msg = `已删除 ${data.deleted_count} 条`;
        if (data.not_found.length > 0) {
            msg += `，${data.not_found.length} 条未找到已跳过`;
        }
        setMsg(formMsg, msg, "success");
        await loadList();
    } catch (e) {
        setMsg(formMsg, e.message, "error");
    } finally {
        batchDeleteBtn.textContent = originalText;
        updateBatchBar();
    }
});

// 新增
form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
        title: document.getElementById("title").value.trim(),
        content: document.getElementById("content").value,
        source: document.getElementById("source").value.trim() || null,
    };
    setMsg(formMsg, "保存中...", "");
    try {
        const res = await fetch(API, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `保存失败 (${res.status})`);
        }
        setMsg(formMsg, "已添加到知识库", "success");
        form.reset();
        await loadList();
    } catch (e) {
        setMsg(formMsg, e.message, "error");
    }
});

// 列表事件委托：编辑 / 删除
tbody.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const id = btn.dataset.id;
    const action = btn.dataset.action;

    if (action === "del") {
        if (!confirm("确定删除这条知识？")) return;
        try {
            const res = await fetch(`${API}/${encodeURIComponent(id)}`, { method: "DELETE" });
            if (!res.ok && res.status !== 204) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `删除失败 (${res.status})`);
            }
            await loadList();
        } catch (e) {
            alert(e.message);
        }
    } else if (action === "edit") {
        try {
            const res = await fetch(`${API}/${encodeURIComponent(id)}`);
            if (!res.ok) throw new Error(`加载失败 (${res.status})`);
            const item = await res.json();
            document.getElementById("editId").value = item.id;
            document.getElementById("editTitle").value = item.title;
            document.getElementById("editSource").value = item.source || "";
            document.getElementById("editContent").value = item.content;
            setMsg(editMsg, "", "");
            modal.classList.remove("hidden");
        } catch (e) {
            alert(e.message);
        }
    }
});

// 编辑提交
editForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = document.getElementById("editId").value;
    const payload = {
        title: document.getElementById("editTitle").value.trim(),
        source: document.getElementById("editSource").value.trim() || null,
        content: document.getElementById("editContent").value,
    };
    setMsg(editMsg, "保存中...", "");
    try {
        const res = await fetch(`${API}/${encodeURIComponent(id)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `保存失败 (${res.status})`);
        }
        modal.classList.add("hidden");
        await loadList();
    } catch (e) {
        setMsg(editMsg, e.message, "error");
    }
});

cancelEdit.addEventListener("click", () => modal.classList.add("hidden"));
refreshBtn.addEventListener("click", loadList);
keywordInput.addEventListener("input", () => {
    clearTimeout(window.__t);
    window.__t = setTimeout(loadList, 300);
});

// ===== 文件上传 =====
const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const fileListEl = document.getElementById("fileList");
const uploadForm = document.getElementById("uploadForm");
const uploadMsg = document.getElementById("uploadMsg");
const clearFilesBtn = document.getElementById("clearFilesBtn");
const uploadBtn = document.getElementById("uploadBtn");
const SUPPORTED = [".txt", ".md", ".markdown", ".pdf"];
let pendingFiles = []; // 待上传文件队列

function setUploadMsg(text, type = "") {
    uploadMsg.textContent = text;
    uploadMsg.className = "msg " + type;
}

function fmtSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function hasSupportedExt(name) {
    const dot = name.slice(name.lastIndexOf(".")).toLowerCase();
    return SUPPORTED.includes(dot);
}

function renderFileList() {
    if (!pendingFiles.length) {
        fileListEl.innerHTML = "";
        return;
    }
    fileListEl.innerHTML = pendingFiles
        .map((f, i) => `
            <div class="file-item" data-idx="${i}">
                <span class="name">📄 ${escapeHtml(f.name)} <span class="size">${fmtSize(f.size)}</span></span>
                <span>
                    <span class="status" data-status-idx="${i}"></span>
                    <span class="remove" data-remove-idx="${i}" title="移除">✕</span>
                </span>
            </div>
        `)
        .join("");
}

function addFiles(files) {
    let added = 0;
    let skipped = 0;
    for (const f of files) {
        if (!hasSupportedExt(f.name)) {
            skipped++;
            continue;
        }
        pendingFiles.push(f);
        added++;
    }
    renderFileList();
    if (skipped > 0) {
        setUploadMsg(`已跳过 ${skipped} 个不支持的文件（仅支持 ${SUPPORTED.join(" / ")}）`, "error");
    } else if (added > 0) {
        setUploadMsg(`已选择 ${added} 个文件，点击"开始上传"`, "");
    }
}

dropZone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = ""; // 允许重复选择同一文件
});

["dragenter", "dragover"].forEach((ev) => {
    dropZone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
});
["dragleave", "drop"].forEach((ev) => {
    dropZone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
    });
});
dropZone.addEventListener("drop", (e) => {
    const files = e.dataTransfer?.files;
    if (files && files.length) addFiles(files);
});

fileListEl.addEventListener("click", (e) => {
    const rm = e.target.closest("[data-remove-idx]");
    if (!rm) return;
    const idx = parseInt(rm.dataset.removeIdx, 10);
    pendingFiles.splice(idx, 1);
    renderFileList();
    setUploadMsg("", "");
});

clearFilesBtn.addEventListener("click", () => {
    pendingFiles = [];
    renderFileList();
    setUploadMsg("", "");
});

uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!pendingFiles.length) {
        setUploadMsg("请先选择文件", "error");
        return;
    }
    uploadBtn.disabled = true;
    setUploadMsg(`开始上传 ${pendingFiles.length} 个文件...`, "");

    let totalChunks = 0;
    let okCount = 0;
    let failCount = 0;
    for (let i = 0; i < pendingFiles.length; i++) {
        const f = pendingFiles[i];
        const statusEl = fileListEl.querySelector(`[data-status-idx="${i}"]`);
        if (statusEl) {
            statusEl.textContent = "上传中...";
            statusEl.className = "status";
        }
        const fd = new FormData();
        fd.append("file", f, f.name);
        try {
            const res = await fetch(`${API}/upload`, { method: "POST", body: fd });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `上传失败 (${res.status})`);
            }
            const data = await res.json();
            totalChunks += data.chunks || 0;
            okCount++;
            if (statusEl) {
                statusEl.textContent = `✓ ${data.chunks} 段`;
                statusEl.className = "status success";
            }
        } catch (err) {
            failCount++;
            if (statusEl) {
                statusEl.textContent = `✕ ${err.message}`;
                statusEl.className = "status error";
            }
        }
    }

    uploadBtn.disabled = false;
    const msgType = failCount > 0 ? "error" : "success";
    const msgText = failCount > 0
        ? `完成：成功 ${okCount} 个，失败 ${failCount} 个，共写入 ${totalChunks} 段`
        : `上传完成：${okCount} 个文件，共写入 ${totalChunks} 段知识`;
    setUploadMsg(msgText, msgType);

    if (okCount > 0) {
        // 成功的清掉，失败的留在列表便于重试
        pendingFiles = pendingFiles.filter((_, idx) => {
            const s = fileListEl.querySelector(`[data-status-idx="${idx}"]`);
            return s && !s.classList.contains("success");
        });
        renderFileList();
        await loadList();
    }
});

loadList();
