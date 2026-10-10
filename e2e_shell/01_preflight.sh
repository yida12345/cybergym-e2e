#!/usr/bin/env bash
# 01_preflight.sh：核对依赖、Docker context、存储驱动、数据、映射与资源预算。
# 只读检查；不改变 Docker、网络或宿主 sysctl。
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source "${script_dir}/00_config.sh"

fail() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARN: $*" >&2; }

echo "== 1/8 外壳依赖 =="
command -v docker >/dev/null 2>&1 || fail "未找到 docker CLI"
"${SHELL_PYTHON}" - <<'PY' || fail "外壳 Python 版本过低（需要 3.11+，stdlib 需要 tomllib）"
import sys
if sys.version_info < (3, 11):
    raise SystemExit(1)
PY
if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
  echo "venv: ${SCRIPT_DIR}/.venv/bin/python"
else
  warn "外壳 venv 未创建；正式运行前执行 requirements.txt 安装（见 README）"
fi
if [[ ! -e "${CC_RUNTIME_DIR}/manifest.json" ]]; then
  warn "CC runtime 未准备: ${CC_RUNTIME_DIR}；运行 python prepare_cc_runtime.py --version ${CLAUDE_CODE_VERSION}"
fi

echo "== 2/8 Docker context 与权限 =="
docker info --format '{{.ServerVersion}} / storage={{.Driver}}' 2>/dev/null || fail "docker daemon 不可达"
docker ps >/dev/null 2>&1 || fail "当前用户无法访问 Docker（需要 docker 组或 root）"
STORAGE_DRIVER="$(docker info --format '{{.Driver}}' 2>/dev/null)"
if [[ ${STORAGE_DRIVER} != overlay2 ]]; then
  warn "存储驱动为 ${STORAGE_DRIVER}（非 overlay2）：容器可写层 storage-opt 硬限额可能不可用，将采用 watchdog"
fi
if ! iptables -L >/dev/null 2>&1; then
  fail "iptables 不可用；网络隔离需要相应权限"
fi

echo "== 3/8 官方仓库与镜像映射 =="
[[ -f "${SOURCE_TASK_FILE}" ]] || fail "官方任务清单不存在: ${SOURCE_TASK_FILE}"
[[ -f "${IMAGE_MAPPING_FILE}" ]] || fail "ACR 映射不存在: ${IMAGE_MAPPING_FILE}"
"${SHELL_PYTHON}" - "${IMAGE_MAPPING_FILE}" <<'PY' || fail "ACR 映射不完整"
import json, sys
payload = json.load(open(sys.argv[1]))
if payload.get("complete") is not True:
    raise SystemExit(1)
PY

echo "== 4/8 数据集资产 =="
if [[ -d "${DATASET_DIR}/projects" ]]; then
  echo "数据集根: ${DATASET_DIR}/projects"
else
  echo "WARN: ${DATASET_DIR}/projects 不存在；官方数据尚未下载（见 DOWNLOAD_AND_RUN.md 第 2 节）"
  echo "     02 冻结前必须完成下载，否则任务数为 0。"
fi

echo "== 5/8 端口与现有 ExploitGym 共存 =="
if ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":${CAPTURE_PROXY_PORT}\$"; then
  fail "capture 端口 ${CAPTURE_PROXY_PORT} 已被占用；检查是否属于其他 run 或更换 CAPTURE_PROXY_PORT"
fi
ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":30304\$" && echo "ExploitGym 30304 在运行（只读记录，不受影响）"
ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":18667\$" && echo "ExploitGym 18667 在运行（只读记录，不受影响）"

echo "== 6/8 资源预算（当前实际读取，不使用历史快照） =="
"${SHELL_PYTHON}" - "${WORKERS}" "${AGENT_CPUS}" "${VALIDATION_CPUS}" "${AGENT_MEMORY}" \
  "${VALIDATION_MEMORY}" "${HOST_RESERVED_CPUS}" "${HOST_RESERVED_MEMORY_GB}" \
  "${ONLINE_WORKERS}" "${MODEL}" <<'PY'
import os, subprocess, sys
workers, agent_cpus, val_cpus = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
agent_mem_gb = float(sys.argv[4].rstrip("gGmM")) if sys.argv[4].endswith(("g","G")) else float(sys.argv[4])/1024
val_mem_gb = float(sys.argv[5].rstrip("gGmM")) if sys.argv[5].endswith(("g","G")) else float(sys.argv[5])/1024
reserved_cpus, reserved_mem = float(sys.argv[6]), float(sys.argv[7])
online_workers = int(sys.argv[8])
model = sys.argv[9]

total = open("/proc/cpuinfo").read().count("processor\t")
mem_kb = open("/proc/meminfo").read().split("MemTotal:")[1].split()[0]
total_mem_gb = int(mem_kb) / 1024 / 1024

# 当前活动容器的配额总额（最坏预算；配额不等于实际使用量）
running = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True).stdout.split()
committed_cpu = 0.0
committed_mem_gb = 0.0
for container in running:
    info = subprocess.run(["docker", "inspect", "--format",
                           "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}", container],
                          capture_output=True, text=True).stdout.split()
    if len(info) == 2:
        committed_cpu += int(info[0]) / 1e9
        committed_mem_gb += int(info[1]) / 1024**3

# E2E 阶段顺序执行且提前回收 Agent 容器：每 worker 峰值按验证内存估算
need_cpu = workers * max(agent_cpus, val_cpus) + 2.0 * online_workers
need_mem = workers * max(agent_mem_gb, val_mem_gb) + 2 * 4
free_cpu = total - reserved_cpus
free_mem = total_mem_gb - reserved_mem
print(f"host: cpu={total} mem={total_mem_gb:.0f}GiB")
print(f"existing container quotas: cpu={committed_cpu:.0f} mem={committed_mem_gb:.0f}GiB")
print(f"e2e request: cpu={need_cpu:.0f} mem={need_mem:.0f}GiB (worst-case per worker)")
print(f"after reserve: cpu={free_cpu:.0f} mem={free_mem:.0f}GiB")
problems = []
if need_cpu > free_cpu:
    problems.append(f"CPU 预算不足: 需要 {need_cpu:.0f} > 可用 {free_cpu:.0f}")
if need_mem > free_mem:
    problems.append(f"内存预算不足: 需要 {need_mem:.0f} > 可用 {free_mem:.0f}")
if problems:
    print("ERROR: " + "; ".join(problems))
    print("       降低 WORKERS 或调整 AGENT/VALIDATION_MEMORY 后重试")
    raise SystemExit(1)
print("resource budget: OK")
PY

echo "== 7/8 宿主 sysctl（只读记录，不修改） =="
echo "vm.mmap_rnd_bits=$(sysctl -n vm.mmap_rnd_bits 2>/dev/null || echo unknown)"
echo "kernel.randomize_va_space=$(sysctl -n kernel.randomize_va_space 2>/dev/null || echo unknown)"

echo "== 8/8 模型凭据 =="
if [[ -z "${!MODEL_API_KEY_ENV:-}" ]]; then
  fail "环境变量 ${MODEL_API_KEY_ENV} 未设置；模型 key 通过环境提供，不写入 config"
fi
echo "preflight 通过。"
