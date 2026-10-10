#!/usr/bin/env bash
# 07_finalize_and_summarize.sh：离线汇总：聚合所有 launch，适配结果、关联轨迹、
# 完整性分析、ShareGPT 与分母分离统计。要求队列 COMPLETE（--snapshot 可部分汇总）。
# 输出写入新的版本目录，不覆盖旧产物；导出失败不会把已完成任务放回队列。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

if [[ ! -f "${QUEUE_DB}" ]]; then
  echo "ERROR: 队列不存在: ${QUEUE_DB}" >&2
  exit 1
fi

extra=()
if [[ "${1:-}" == "--snapshot" ]]; then
  extra+=(--snapshot)
fi

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" finalize "${extra[@]+"${extra[@]}"}"
