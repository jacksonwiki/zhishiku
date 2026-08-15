#!/usr/bin/env bash
# 停止 RAG 知识库服务。
# 用法：./scripts/stop.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"

PID="$(read_pid || true)"

# 兜底：未记录 PID 时按端口查找
if [[ -z "${PID:-}" ]]; then
    c_warn "未找到 PID 文件，服务可能未运行"
    if command -v lsof >/dev/null 2>&1; then
        PORT_PID="$(lsof -ti tcp:"${APP_PORT}" 2>/dev/null || true)"
        if [[ -n "${PORT_PID:-}" ]]; then
            PID="${PORT_PID}"
            c_info "在端口 ${APP_PORT} 发现进程 PID=${PID}，尝试停止"
        fi
    fi
    if [[ -z "${PID:-}" ]]; then
        exit 0
    fi
fi

if ! is_alive "${PID}"; then
    c_warn "PID=${PID} 已不在运行，清理 PID 文件"
    rm -f "${PID_FILE}"
    exit 0
fi

c_info "正在停止服务 PID=${PID} ..."
kill "${PID}" 2>/dev/null || true

# 优雅等待最多 10 秒
for _ in $(seq 1 20); do
    if ! is_alive "${PID}"; then
        c_ok "服务已停止 (PID=${PID})"
        rm -f "${PID_FILE}"
        exit 0
    fi
    sleep 0.5
done

# 仍未退出则强制 kill
c_warn "进程未响应 SIGTERM，发送 SIGKILL"
kill -9 "${PID}" 2>/dev/null || true
sleep 1
if is_alive "${PID}"; then
    c_err "无法停止进程 PID=${PID}，请手动处理"
    exit 1
fi
rm -f "${PID_FILE}"
c_ok "服务已强制停止 (PID=${PID})"
