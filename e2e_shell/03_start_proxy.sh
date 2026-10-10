#!/usr/bin/env bash
# 03_start_proxy.sh：为 launch 启动独立 capture 进程（端口 30305）。
# 保存 PID/start、日志根和健康配置；端口被未知同位进程占用时报错。
# 幂等：本 launch 已有健康代理时复用。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

[[ -f "${STATE_DIR}/network.json" ]] || {
  echo "ERROR: 网络未就绪，先运行 03_start_network.sh" >&2
  exit 1
}
[[ -f "${CAPTURE_PROXY_ENTRYPOINT}" ]] || {
  echo "ERROR: capture 代理入口不存在: ${CAPTURE_PROXY_ENTRYPOINT}" >&2
  exit 1
}

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" proxy-up
