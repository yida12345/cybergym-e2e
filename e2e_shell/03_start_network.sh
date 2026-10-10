#!/usr/bin/env bash
# 03_start_network.sh：创建带 scope labels 的 E2E 独立网络与 Squid，
# 添加 internal 网桥到宿主 INPUT 的精确阻断规则。
# 已存在时核对所有权复用；部分失败回滚本次资源。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

if [[ -f "${STATE_DIR}/network.json" ]]; then
  echo "scope 网络已存在，复用: $(cat "${STATE_DIR}/network.json")"
  exit 0
fi

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" network-up
