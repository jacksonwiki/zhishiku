#!/usr/bin/env bash
# 公共配置：被 start/stop/restart 脚本 source 引入。
# 这里集中管理 Python 解释器选择、路径、端口、PID 与日志文件位置。

# 项目根目录（脚本位于 scripts/ 下）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# PID 文件与日志文件
PID_FILE="$PROJECT_ROOT/.run/app.pid"
LOG_FILE="$PROJECT_ROOT/.run/app.log"
RUN_DIR="$(dirname "$PID_FILE")"

# 应用配置（与 app/config.py 默认一致；可在 .env 覆盖）
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"

# 选择 Python 解释器：
#   1) 优先使用项目根目录下的 .venv（若存在且可用）
#   2) 否则回退到 PATH 中的 python3
select_python() {
    local venv_python="$PROJECT_ROOT/.venv/bin/python"
    if [[ -x "$venv_python" ]]; then
        # 检查 venv 是否安装了 fastapi（uv 创建的精简 venv 可能没有）
        if "$venv_python" -c "import fastapi, uvicorn, langchain" >/dev/null 2>&1; then
            echo "$venv_python"
            return 0
        fi
    fi
    # 回退：系统 Python
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
        return 0
    fi
    echo ""
    return 1
}

# 确保运行目录存在
ensure_run_dir() {
    mkdir -p "$RUN_DIR"
}

# 读取已记录的 PID（不存在则返回空）
read_pid() {
    if [[ -f "$PID_FILE" ]]; then
        cat "$PID_FILE" 2>/dev/null
    fi
}

# 判断某个 PID 是否仍在运行
is_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# 颜色输出
c_info()  { printf "\033[36m[INFO]\033[0m  %s\n" "$*"; }
c_ok()    { printf "\033[32m[OK]\033[0m    %s\n" "$*"; }
c_warn()  { printf "\033[33m[WARN]\033[0m  %s\n" "$*"; }
c_err()   { printf "\033[31m[ERR]\033[0m   %s\n" "$*" >&2; }
