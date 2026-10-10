#!/usr/bin/env bash
# run_all.sh：自动判断新实验／活动／停止／完成，并按序准备、启动。
# 用法：
#   bash run_all.sh                # 正式启动（含连通性检查）
#   bash run_all.sh --dry-run      # 只打印计划；不创建任务容器、不发模型请求
#   bash run_all.sh --network-reverse-test   # 启动前执行网络反向测试
#   bash run_all.sh --no-smoke     # 跳过最小模型连通性检查（不建议）
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

usage() { echo "Usage: bash run_all.sh [--dry-run] [--network-reverse-test] [--no-smoke]" >&2; }

RUN_NETWORK_REVERSE_TEST=false
RUN_SMOKE=true
DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --network-reverse-test) RUN_NETWORK_REVERSE_TEST=true; shift ;;
    --no-smoke) RUN_SMOKE=false; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: 未知参数: $1" >&2; usage; exit 2 ;;
  esac
done
export RUN_NETWORK_REVERSE_TEST

if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" && ${DRY_RUN} == false ]]; then
  echo "ERROR: 未找到外壳虚拟环境 ${SCRIPT_DIR}/.venv；请先:" >&2
  echo "  python3 -m venv ${SCRIPT_DIR}/.venv && ${SCRIPT_DIR}/.venv/bin/pip install -r ${SCRIPT_DIR}/requirements.txt" >&2
  exit 1
fi

# 持有 scope 启动锁后判断实验状态。
if [[ ${DRY_RUN} == true ]]; then
  "${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" preflight-report
  echo "dry-run 结束：未创建任务容器、未发送模型请求。"
  exit 0
fi

# 1) 依赖与资源检查（只读；不改 Docker/网络/sysctl）
bash "${script_dir}/01_preflight.sh"

# 2) 冻结输入（新实验）或核对复用（续跑）
bash "${script_dir}/02_prepare_dataset.sh"

# 3) scope 网络与独立 capture
bash "${script_dir}/03_start_network.sh"
bash "${script_dir}/03_start_proxy.sh"

# 4) 连通性与反向测试
if [[ ${RUN_NETWORK_REVERSE_TEST} == true ]]; then
  bash "${script_dir}/network_reverse_test.sh"
fi
if [[ ${RUN_SMOKE} == true ]]; then
  bash "${script_dir}/04_smoke_test.sh"
fi

# 5) 启动 supervisor（内部完成新建/活动/停止/完成四种状态的判断）
bash "${script_dir}/05_start_evaluation.sh"

echo ""
echo "启动完成；后台 supervisor 已运行。"
echo "  状态:   bash ${script_dir}/06_status.sh --watch"
echo "  停止:   bash ${script_dir}/08_stop.sh"
echo "  接续:   bash ${script_dir}/run_all.sh   （同 EXP/round，不改 config）"
