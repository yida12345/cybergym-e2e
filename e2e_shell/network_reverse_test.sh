#!/usr/bin/env bash
# network_reverse_test.sh：在同配置测试容器中核对允许和被阻断的路径。
# 允许：internal 网络容器经 Squid 访问 capture。阻断：直连 capture 地址、
# 访问公网。使用独立角色与日志，结束后清理本 scope 测试容器。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

[[ -f "${STATE_DIR}/services.json" ]] || {
  echo "ERROR: 网络未就绪，先运行 03_start_network.sh 与 03_start_proxy.sh" >&2
  exit 1
}

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" reverse-test
