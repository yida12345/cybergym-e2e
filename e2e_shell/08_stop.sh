#!/usr/bin/env bash
# 08_stop.sh：停止领取，保存并中断活动 attempt，回收本 scope 资源。
# 保留队列、manifest、原始 capture、历史结果与中断记录；下次 run_all 接续。
# 幂等；区分“停止运行资源”与“删除历史数据”——默认不删除任何历史数据。
# 可选 --cleanup-images：额外清理本轮 owned 且无消费者的镜像。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

args=()
if [[ "${1:-}" == "--cleanup-images" ]]; then
  args+=(--cleanup-images)
fi

if [[ ! -f "${QUEUE_DB}" ]]; then
  echo "队列不存在（${QUEUE_DB}）；仅回收 scope 服务资源。"
  "${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" proxy-stop || true
  "${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" network-down || true
  exit 0
fi

"${SHELL_PYTHON}" "${SCRIPT_DIR}/orchestrator.py" stop "${args[@]+"${args[@]}"}"

echo ""
echo "停止完成。历史数据（队列/冻结输入/capture/attempts）全部保留。"
echo "接续运行: bash ${script_dir}/run_all.sh    （保持同一 config/EXP/round）"
