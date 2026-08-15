#!/usr/bin/env bash
# 重启 RAG 知识库服务。
# 用法：
#   ./scripts/restart.sh              前台重启
#   ./scripts/restart.sh -d           后台重启
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"

c_info "停止旧实例..."
# stop.sh 退出码 0 表示成功停止或本就没运行；非 0 才视为真正失败
if ! bash "$SCRIPT_DIR/stop.sh"; then
    echo "停止失败，继续尝试启动" >&2
fi

c_info "启动新实例..."
exec bash "$SCRIPT_DIR/start.sh" "$@"
