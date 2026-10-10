#!/usr/bin/env bash
# 05_start_evaluation.sh：初始化或恢复 queue，启动单 supervisor 和 worker 池。
# 不通过原生 batch_run 再扩并发；重复启动被 scope 启动锁拒绝。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

[[ -f "${STATE_DIR}/services.json" ]] || {
  echo "ERROR: capture 服务未就绪，先运行 03 系列脚本" >&2
  exit 1
}
[[ -f "${QUEUE_DB}" ]] || {
  echo "ERROR: 队列未初始化，先运行 02_prepare_dataset.sh" >&2
  exit 1
}

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" launch
