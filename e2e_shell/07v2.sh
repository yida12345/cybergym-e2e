#!/usr/bin/env bash
# 07v2.sh：在线增量导出（不停止 benchmark）。
#   bash 07v2.sh                  处理一次增量
#   bash 07v2.sh --watch          监控模式
#   bash 07v2.sh --watch --interval 300
# 使用独立锁与不可变缓存；同一输出目录的第二个实例被拒绝；
# 中断不影响评测。结果为 provisional，完成后用 07 做最终核对。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

if [[ ! -f "${QUEUE_DB}" ]]; then
  echo "ERROR: 队列不存在: ${QUEUE_DB}" >&2
  exit 1
fi

args=()
if [[ "${1:-}" == "--watch" ]]; then
  args+=(--watch)
  shift || true
fi
if [[ "${1:-}" == "--interval" && -n "${2:-}" ]]; then
  args+=(--interval "$2")
fi

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" online "${args[@]+"${args[@]}"}"
