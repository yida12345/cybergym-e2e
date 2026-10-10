#!/usr/bin/env bash
# 09_reclassify.sh：按新的 --success-stages 重新分组。
# 只读已保存的阶段证据，不运行模型或验证，不改写原始评分；输出到独立
# policy 目录。引用未执行阶段时给出不可评估与覆盖率。
# 示例：
#   bash 09_reclassify.sh --success-stages "1 2 3 4"
#   bash 09_reclassify.sh --success-stages "3 4"
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

if [[ ! -f "${QUEUE_DB}" ]]; then
  echo "ERROR: 队列不存在: ${QUEUE_DB}" >&2
  exit 1
fi

stages=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --success-stages) stages="${2:-}"; shift 2 ;;
    *) echo "ERROR: 未知参数: $1" >&2; exit 2 ;;
  esac
done
[[ -n "${stages}" ]] || { echo "用法: bash 09_reclassify.sh --success-stages \"1 2 3\"" >&2; exit 2; }

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" reclassify --success-stages "${stages}"
