#!/usr/bin/env bash
# 02_prepare_dataset.sh：新建时冻结最终任务、脚本、TOML、资产和代码指纹；
# resume 只核对并复用（不重新随机排序、不刷新远程 main、不覆盖旧配置）。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

if [[ ! -f "${SOURCE_TASK_FILE}" ]]; then
  echo "ERROR: 官方任务清单不存在: ${SOURCE_TASK_FILE}" >&2
  exit 1
fi

if [[ -f "${QUEUE_DB}" ]]; then
  # 已有队列：核对冻结输入与当前配置一致，然后复用。
  "${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" resume
  echo "冻结输入核对通过，复用已冻结任务清单。"
else
  "${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" init
  echo "新实验：输入已冻结并初始化队列。"
fi
