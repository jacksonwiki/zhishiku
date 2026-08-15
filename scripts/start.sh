#!/usr/bin/env bash
# 启动 RAG 知识库服务。
# 用法：
#   ./scripts/start.sh              前台启动（日志直接输出到终端）
#   ./scripts/start.sh -d           后台启动（日志写入 .run/app.log）
#   APP_PORT=9000 ./scripts/start.sh -d   指定端口
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"

DAEMON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--daemon) DAEMON=1; shift ;;
        -h|--help)
            sed -n '2,6p' "$0"
            exit 0
            ;;
        *) c_err "未知参数: $1"; exit 1 ;;
    esac
done

# 已有实例则直接退出
existing_pid="$(read_pid)"
if is_alive "$existing_pid"; then
    c_warn "服务已在运行 (PID=$existing_pid)，端口 $APP_PORT"
    exit 0
fi

# 残留 PID 文件清理
if [[ -f "$PID_FILE" ]]; then
    c_warn "发现残留 PID 文件，已清理"
    rm -f "$PID_FILE"
fi

PYTHON_BIN="$(select_python)" || {
    c_err "未找到可用的 Python 解释器"
    exit 1
}

# 检查依赖是否就绪
if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, langchain, langchain_chroma, langchain_community, dotenv" >/dev/null 2>&1; then
    c_err "Python 依赖未安装完整，请先执行："
    c_err "  $PYTHON_BIN -m pip install -e ."
    exit 1
fi

# 检查 .env 是否存在
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
    c_warn "未找到 .env，将从 .env.example 复制一份（请填入真实 DASHSCOPE_API_KEY）"
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
fi

ensure_run_dir

cd "$PROJECT_ROOT"

c_info "Python : $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
c_info "Host   : $APP_HOST"
c_info "Port   : $APP_PORT"
c_info "Log    : $LOG_FILE"

if [[ "$DAEMON" -eq 1 ]]; then
    # 后台启动：nohup + 重定向
    nohup "$PYTHON_BIN" -m uvicorn app.main:app \
        --host "$APP_HOST" --port "$APP_PORT" \
        > "$LOG_FILE" 2>&1 &
    APP_PID=$!
    echo "$APP_PID" > "$PID_FILE"

    # 等待最多 8 秒确认启动成功
    c_info "后台启动中 (PID=$APP_PID)，等待健康检查..."
    for i in $(seq 1 16); do
        if ! is_alive "$APP_PID"; then
            c_err "进程已退出，请查看日志：tail -n 50 $LOG_FILE"
            tail -n 10 "$LOG_FILE" 2>/dev/null || true
            rm -f "$PID_FILE"
            exit 1
        fi
        if curl -sf "http://$APP_HOST:$APP_PORT/api/health" >/dev/null 2>&1; then
            c_ok "服务已启动 (PID=$APP_PID)"
            c_ok "访问维护页: http://$APP_HOST:$APP_PORT/"
            c_ok "访问检索页: http://$APP_HOST:$APP_PORT/search"
            exit 0
        fi
        sleep 0.5
    done
    c_warn "服务进程在运行，但健康检查未通过，请稍后再试或查看日志："
    c_warn "  tail -f $LOG_FILE"
    exit 0
else
    # 前台启动：日志直接打印到终端
    c_info "前台启动（Ctrl+C 退出）"
    exec "$PYTHON_BIN" -m uvicorn app.main:app \
        --host "$APP_HOST" --port "$APP_PORT"
fi
