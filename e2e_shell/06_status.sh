#!/usr/bin/env bash
# 06_status.sh：只读队列状态面板。
#   bash 06_status.sh            单次展示
#   bash 06_status.sh --watch    持续刷新
#   bash 06_status.sh --wait     等待终态（COMPLETE/STOPPED/ATTENTION）
#   bash 06_status.sh --json     JSON 输出
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

if [[ ! -f "${QUEUE_DB}" ]]; then
  echo "队列不存在: ${QUEUE_DB}（尚未初始化实验）" >&2
  exit 1
fi

exec "${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" status "$@"
