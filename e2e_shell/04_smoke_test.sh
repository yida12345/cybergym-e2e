#!/usr/bin/env bash
# 04_smoke_test.sh：核对 CC 模型路由、采集和重试设置。
# 发送一条最小模型请求（标为 auxiliary，capture 单列，不混入 benchmark），
# 并核对 REASONING_EFFORT 在 capture 请求体中的实际字段与上游支持情况。
# 失败不启动任务池。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

[[ -f "${STATE_DIR}/services.json" ]] || {
  echo "ERROR: capture 服务未就绪，先运行 03 系列脚本" >&2
  exit 1
}

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" smoke
