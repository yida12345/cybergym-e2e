#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# CyberGym-E2E 本地评测外壳配置
#
# 全部可修改参数集中在本文件顶部，每项均有中文注释说明含义、单位和约束。
# 底部是派生路径与内部环境变量导出，通常无需修改。
# 所有参数支持环境变量覆盖（如 `WORKERS=4 bash run_all.sh`）。
# 凭据（API key 等）不写在本文件中，通过环境变量提供。
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 外壳独立 Python 环境（虚拟环境）；不使用 ExploitGym 的 arvo 环境。
SHELL_VENV="${SHELL_VENV:-${SCRIPT_DIR}/.venv}"
SHELL_PYTHON="${SHELL_PYTHON:-${SHELL_VENV}/bin/python}"
# 未创建 venv 时退回当前 python3（仅 dry-run / 状态查询可用；正式运行需要 venv）。
if [[ ! -x ${SHELL_PYTHON} ]]; then
  SHELL_PYTHON="$(command -v python3)"
fi

# -----------------------------------------------------------------------------
# 一、实验与输入路径
# -----------------------------------------------------------------------------

# 实验名：同一实验 + 轮次唯一定义输出目录。停止后接续时保持不变；
# 改变模型、任务清单、阶段配置等核心参数需要新实验（新 EXP 或新轮次）。
# 只允许字母、数字、下划线、点和连字符。
EXP="${EXP:-e2e_glm53_first}"
# 轮次编号（正整数）。同一 EXP 下不同轮次完全隔离。
ROUND_NUMBER="${ROUND_NUMBER:-1}"
# 所有数据、工具和实验输出的根目录（必须是绝对路径）。
OUTPUT_ROOT="${OUTPUT_ROOT:-/data/cybergym-e2e/output}"
# 官方 cybergym-e2e 仓库位置（只读复用：提示、任务脚本、validate.py）。
REPO_DIR="${REPO_DIR:-/data/cybergym-e2e/cybergym-e2e}"
# 官方数据集下载根目录；内部应包含 projects/<project>/<task>/{src.tgz,poc.bin,crash.log}。
DATASET_DIR="${DATASET_DIR:-${OUTPUT_ROOT}/dataset}"
# 官方任务清单（每行 project/task）。
SOURCE_TASK_FILE="${SOURCE_TASK_FILE:-${REPO_DIR}/scripts/tasks.txt}"
# 已有 ACR digest 映射（镜像迁移工具产出；外壳按需读取，不重复全量迁移）。
IMAGE_MAPPING_FILE="${IMAGE_MAPPING_FILE:-/data/cybergym-e2e/acr_image_mirror/runs/default/image_mapping.json}"
# 可选：只选择这些项目（逗号或空格分隔，如 "curl,ffmpeg"）。留空选择全部。
INCLUDE_PROJECTS="${INCLUDE_PROJECTS:-}"
# 可选：只选择这些任务（project/task 形式）。留空选择全部。
INCLUDE_TASKS="${INCLUDE_TASKS:-}"
# 可选：历史已完成任务清单文件；冻结时从本轮任务中精确排除。
EXCLUDE_COMPLETED_TASK_FILE="${EXCLUDE_COMPLETED_TASK_FILE:-}"
# 冻结任务清单前是否随机排序（true/false）。排序使用固定种子，续跑不重排。
SHUFFLE_TASKS="${SHUFFLE_TASKS:-true}"
# 随机排序种子（任意整数）。
SHUFFLE_SEED="${SHUFFLE_SEED:-42}"
# 数据冻结方式：hardlink（同文件系统硬链接大文件，省空间）或 copy（完整复制）。
# 硬链接不是内容不可变保障：源资产变更会在续跑核验时被发现并拒绝。
DATA_FREEZE_MODE="${DATA_FREEZE_MODE:-hardlink}"

# -----------------------------------------------------------------------------
# 二、模式与阶段（S1=Agent PoC 触发崩溃；S2=补丁修复该 PoC；S3=项目测试通过；
#     S4=参考 PoC 被补丁修复）
# -----------------------------------------------------------------------------

# 评测模式：e2e（Agent 自己生成 PoC 与补丁）或 patch-only（给定崩溃日志与
# PoC，只生成补丁；该模式不支持 S1/S2）。
MODE="${MODE:-e2e}"
# 启用的验证阶段：auto（按 MODE 取默认）或空格/逗号分隔的 1-4 编号。
# 所有启用阶段独立执行：前一阶段失败不会跳过后续阶段；每阶段全新容器。
ENABLED_STAGES="${ENABLED_STAGES:-auto}"
# 成功分组条件：auto 或阶段编号子集。这些阶段必须全部 passed 才算成功；
# 必须是 ENABLED_STAGES 的非空子集。
SUCCESS_STAGES="${SUCCESS_STAGES:-auto}"
# 默认：e2e → 启用 1 2 3 4、成功要求 1 2 3；patch-only → 启用 3 4、成功要求 3 4。

# -----------------------------------------------------------------------------
# 三、模型与 Claude Code
# -----------------------------------------------------------------------------

# 模型名（透传给上游网关的别名；所有默认模型别名统一为该值）。
MODEL="${MODEL:-glm53}"
# 推理强度：low/medium/high/xhigh/max/auto。默认 high。
# 实际生效以 04 连通性检查在 capture 请求体中核对到的字段为准。
REASONING_EFFORT="${REASONING_EFFORT:-high}"
# Claude Code 版本，固定为官方 E2E 使用的 2.1.91；不自动升级，
# 不复用 ExploitGym 的 2.1.119 工具目录。
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.91}"
# 模型服务提供方；当前规格只实现 anthropic（Anthropic 协议）。
MODEL_PROVIDER="${MODEL_PROVIDER:-anthropic}"
# Agent 后端；当前规格只实现 claude-code。
AGENT="${AGENT:-claude-code}"
# 提示风格：iterative（Agent 可自测，官方默认）或 no-test。
PROMPT_STYLE="${PROMPT_STYLE:-iterative}"
# E2E 独立 CC/Node 工具目录（prepare_cc_runtime.py 产出；只读挂载进容器）。
CC_RUNTIME_DIR="${CC_RUNTIME_DIR:-${OUTPUT_ROOT}/tools/cc-runtime}"
# CC 安装方式：shared-runtime（共享只读工具，默认）或 per-container
# （每容器按官方命令安装固定版本，兼容性失败时的显式备用路径）。
CC_INSTALL_MODE="${CC_INSTALL_MODE:-shared-runtime}"
# 提供 API key 的环境变量名（key 本身不写进 config/manifest/日志）。
MODEL_API_KEY_ENV="${MODEL_API_KEY_ENV:-ANTHROPIC_API_KEY}"

# -----------------------------------------------------------------------------
# 四、并发与重复
# -----------------------------------------------------------------------------

# 同时运行的 worker 数（每个 worker 串行处理任务与 repeat）。
WORKERS="${WORKERS:-8}"
# 每个任务独立重复次数（repeat 之间不共享会话、产物或反馈）。
TASK_REPEAT_N="${TASK_REPEAT_N:-2}"
# 单次 launch 内 worker 异常退出的重启上限；超出进入 ATTENTION。
WORKER_MAX_RESTARTS="${WORKER_MAX_RESTARTS:-3}"
# 续跑策略：auto（识别 stop/stale/complete 并自动接续）。仅 auto 可用。
RESUME_POLICY="${RESUME_POLICY:-auto}"
# 普通基础设施错误处理：record（归档为终态并记录，不无限自动重试）。
INFRA_ERROR_POLICY="${INFRA_ERROR_POLICY:-record}"

# -----------------------------------------------------------------------------
# 五、容器资源（Agent 与验证容器分别设置）
# -----------------------------------------------------------------------------

# Agent 容器 CPU 数。
AGENT_CPUS="${AGENT_CPUS:-2}"
# 验证容器 CPU 数。
VALIDATION_CPUS="${VALIDATION_CPUS:-2}"
# Agent 容器内存（memory-swap 设为相同值，禁用额外 swap）。如 12g / 512m。
AGENT_MEMORY="${AGENT_MEMORY:-12g}"
# 验证容器内存（阶段验证通常需要更大编译内存）。
VALIDATION_MEMORY="${VALIDATION_MEMORY:-16g}"
# Agent 容器进程数上限。
AGENT_PIDS_LIMIT="${AGENT_PIDS_LIMIT:-2048}"
# 验证容器进程数上限。
VALIDATION_PIDS_LIMIT="${VALIDATION_PIDS_LIMIT:-2048}"
# core dump 大小上限（0=禁用）。
AGENT_CORE_LIMIT_BYTES="${AGENT_CORE_LIMIT_BYTES:-0}"

# -----------------------------------------------------------------------------
# 六、时间预算（单位：秒）
# -----------------------------------------------------------------------------

# Agent 推理时间上限（7 天）。
AGENT_TIMEOUT_SECONDS="${AGENT_TIMEOUT_SECONDS:-604800}"
# Agent setup（拉镜像、排队、安装、可信 prepare）预算（3 小时）；
# 启动槽等待计入此处，不挤占 Agent 推理预算。
AGENT_SETUP_TIMEOUT_SECONDS="${AGENT_SETUP_TIMEOUT_SECONDS:-10800}"
# 单阶段 setup 预算（容器创建 + 可信 prepare）。
STAGE_SETUP_TIMEOUT_SECONDS="${STAGE_SETUP_TIMEOUT_SECONDS:-7200}"
# 单阶段验证预算（官方内部 compile 3600s/test 1800s 等限额不变）。
STAGE_VALIDATION_TIMEOUT_SECONDS="${STAGE_VALIDATION_TIMEOUT_SECONDS:-7200}"
# 清理余量（每 attempt 收尾）。
CLEANUP_BUDGET_SECONDS="${CLEANUP_BUDGET_SECONDS:-1800}"
# 08 停止时等待 supervisor 自行退出的时间。
STOP_GRACE_SECONDS="${STOP_GRACE_SECONDS:-60}"
# CC API 超时（毫秒；覆盖完整缓存响应的等待，25 小时）。
AGENT_API_TIMEOUT_MS="${AGENT_API_TIMEOUT_MS:-90000000}"
# CC 层额外重试次数（capture 层另有 20 次完整缓存重试）。
AGENT_API_MAX_RETRIES="${AGENT_API_MAX_RETRIES:-3}"
# 终态 result 事件后允许的静默时长；只按已知终态事件启动计时，
# 不会因正常等待模型响应而误判（0=禁用）。
AGENT_RESULT_IDLE_TIMEOUT_SECONDS="${AGENT_RESULT_IDLE_TIMEOUT_SECONDS:-300}"
# 强制空闲超时（0=默认禁用；仅处理终态 result 后未退出）。
AGENT_API_FORCE_IDLE_TIMEOUT="${AGENT_API_FORCE_IDLE_TIMEOUT:-0}"
# 单次模型输出 token 上限。
AGENT_MAX_OUTPUT_TOKENS="${AGENT_MAX_OUTPUT_TOKENS:-64000}"

# -----------------------------------------------------------------------------
# 七、Docker
# -----------------------------------------------------------------------------

# Docker 单次 API 超时（秒）。冷启动 pull/create/start 可能远超 SDK 默认。
DOCKER_API_TIMEOUT_SECONDS="${DOCKER_API_TIMEOUT_SECONDS:-600}"
# 超时后的额外重试次数（首次之外）。
DOCKER_TIMEOUT_RETRIES="${DOCKER_TIMEOUT_RETRIES:-3}"
# 重试间隔（秒）。
DOCKER_RETRY_DELAY_SECONDS="${DOCKER_RETRY_DELAY_SECONDS:-10}"
# 整机 Docker 启动槽数量（与 ExploitGym/run_arc 共用同一 flock 目录协议）。
DOCKER_STARTUP_CONCURRENCY="${DOCKER_STARTUP_CONCURRENCY:-8}"
# 共享启动锁目录；不要改为各实验独立目录，否则失去整机节流意义。
DOCKER_STARTUP_LOCK_DIR="${DOCKER_STARTUP_LOCK_DIR:-/tmp/cybergym-docker-startup-slots}"

# -----------------------------------------------------------------------------
# 八、资源共存预算（与正在运行的 ExploitGym 共存）
# -----------------------------------------------------------------------------

# 为宿主保留的 CPU 数（启动前重新计算，不使用历史快照）。
HOST_RESERVED_CPUS="${HOST_RESERVED_CPUS:-8}"
# 为宿主保留的内存（GiB）。
HOST_RESERVED_MEMORY_GB="${HOST_RESERVED_MEMORY_GB:-32}"
# 磁盘低水位（GiB）：低于此值停止领取新任务并中断活动 attempt。
MIN_HOST_FREE_GB="${MIN_HOST_FREE_GB:-100}"
# watchdog 昂贵检查（磁盘/产物大小/容器可写层）的间隔（秒）。
DISK_CHECK_INTERVAL_SECONDS="${DISK_CHECK_INTERVAL_SECONDS:-5}"
# 单 attempt 产物目录大小上限（GiB）。
MAX_ATTEMPT_OUTPUT_GB="${MAX_ATTEMPT_OUTPUT_GB:-20}"
# 单 attempt 容器可写层合计上限（GiB）。存储驱动不支持硬限额时（如下文
# 设置了 storage-opt 但驱动不支持），此值由 watchdog 强制执行。
MAX_CONTAINER_RW_GB="${MAX_CONTAINER_RW_GB:-16}"

# -----------------------------------------------------------------------------
# 九、capture 代理与网络
# -----------------------------------------------------------------------------

# capture 代理工具源码目录（只读复用；不修改共享源码）。
CAPTURE_PROXY_DIR="${CAPTURE_PROXY_DIR:-/data/ztr/anthropic_full_capture_proxy}"
# 运行 capture 代理使用的 Python（外壳 venv，需安装其依赖）。
CAPTURE_PROXY_PYTHON="${CAPTURE_PROXY_PYTHON:-${SHELL_PYTHON}}"
# capture 代理入口（完整缓存 + 失败重试版本）。
CAPTURE_PROXY_ENTRYPOINT="${CAPTURE_PROXY_ENTRYPOINT:-${CAPTURE_PROXY_DIR}/proxy_retry.py}"
# E2E 独立 capture 端口（ExploitGym 使用 30304；E2E 使用 30305）。
CAPTURE_PROXY_PORT="${CAPTURE_PROXY_PORT:-30305}"
# 上游路由前缀（保留入站 path，CC 的 /v1/messages 会拼在其后）。
CAPTURE_ROUTE_PREFIX="${CAPTURE_ROUTE_PREFIX:-/glm53}"
# 上游网关地址。
CAPTURE_UPSTREAM_URL="${CAPTURE_UPSTREAM_URL:-http://8.130.156.14:8000}"
# capture 绑定地址；留空时自动使用本 scope services 网络网关。
CAPTURE_BIND_HOST="${CAPTURE_BIND_HOST:-}"
# 代理各级超时（秒）：connect 30、读 600、单次上游 3600、请求总预算 86400。
CAPTURE_TIMEOUT_SECONDS="${CAPTURE_TIMEOUT_SECONDS:-300}"
CAPTURE_CONNECT_TIMEOUT_SECONDS="${CAPTURE_CONNECT_TIMEOUT_SECONDS:-30}"
CAPTURE_READ_TIMEOUT_SECONDS="${CAPTURE_READ_TIMEOUT_SECONDS:-600}"
CAPTURE_ATTEMPT_TIMEOUT_SECONDS="${CAPTURE_ATTEMPT_TIMEOUT_SECONDS:-3600}"
CAPTURE_REQUEST_TIMEOUT_SECONDS="${CAPTURE_REQUEST_TIMEOUT_SECONDS:-86400}"
# 首次之外的最大重试次数（首次 + 重试 ≤ 21 次）。
CAPTURE_MAX_RETRIES="${CAPTURE_MAX_RETRIES:-20}"
# 重试退避初值与上限（秒）；保留现有 Retry-After 处理。
CAPTURE_RETRY_DELAY_SECONDS="${CAPTURE_RETRY_DELAY_SECONDS:-1}"
CAPTURE_RETRY_MAX_DELAY_SECONDS="${CAPTURE_RETRY_MAX_DELAY_SECONDS:-30}"
# 07 自动收尾时等待 inflight capture 的时间（秒）。
CAPTURE_DRAIN_TIMEOUT_SECONDS="${CAPTURE_DRAIN_TIMEOUT_SECONDS:-600}"
# 04 连通性检查的最长等待（秒）。
CAPTURE_SMOKE_TIMEOUT_SECONDS="${CAPTURE_SMOKE_TIMEOUT_SECONDS:-86700}"

# -----------------------------------------------------------------------------
# 十、输出与导出
# -----------------------------------------------------------------------------

# 是否导出 ShareGPT（true/false）。
EXPORT_SHAREGPT="${EXPORT_SHAREGPT:-true}"
# 队列 COMPLETE 后是否自动运行 07 离线收尾。
AUTO_FINALIZE_ON_COMPLETE="${AUTO_FINALIZE_ON_COMPLETE:-true}"
# 是否随 run_all 启动在线导出器（true/false；也可单独运行 07v2.sh）。
ONLINE_EXPORT_ENABLED="${ONLINE_EXPORT_ENABLED:-false}"
# 在线导出并发数（计入共存预算）。
ONLINE_WORKERS="${ONLINE_WORKERS:-2}"
# 在线导出轮询间隔（秒）。
ONLINE_INTERVAL_SECONDS="${ONLINE_INTERVAL_SECONDS:-300}"
# 状态面板刷新间隔（秒）。
STATUS_POLL_SECONDS="${STATUS_POLL_SECONDS:-5}"
# 是否保存完整工作区（true/false；默认只保存候选产物、日志和证据）。
SAVE_FULL_WORKSPACE="${SAVE_FULL_WORKSPACE:-false}"
# 是否启用镜像清理（true/false；清理只作用于本轮 owned 镜像）。
IMAGE_CLEANUP_ENABLED="${IMAGE_CLEANUP_ENABLED:-true}"

# =============================================================================
# 以下为派生路径与内部实现，通常无需修改
# =============================================================================

RUN_SCOPE="${OUTPUT_ROOT}/runs/${EXP}/round_${ROUND_NUMBER}"
STATE_DIR="${RUN_SCOPE}/state"
LOG_DIR="${RUN_SCOPE}/logs"
ATTEMPT_ROOT="${RUN_SCOPE}/attempts"
PID_FILE="${STATE_DIR}/supervisor.pid"
QUEUE_DB="${STATE_DIR}/queue.sqlite3"
CAPTURE_LOG_ROOT="${RUN_SCOPE}/capture_logs"
SHARED_STATE_DIR="${OUTPUT_ROOT}/shared_state"
FINALIZER_SCRIPT="${CAPTURE_PROXY_DIR}/finalize-proc-split.py"
SHAREGPT_EXPORT_SCRIPT="${CAPTURE_PROXY_DIR}/export_sharegpt_recovered.py"
# 只读轨迹完整性分析器（CyberGym 共用，报告单独写入本轮目录）。
TRAJ_ANALYSIS_SCRIPT="${TRAJ_ANALYSIS_SCRIPT:-/data/cybergym/glm_5_1_scripts/traj_analysis.py}"

# scope 前缀：容器/网络命名与所有权 labels 均以 cg-e2e 开头。
export SCRIPT_DIR SHELL_PYTHON
export EXP ROUND_NUMBER OUTPUT_ROOT REPO_DIR DATASET_DIR SOURCE_TASK_FILE
export IMAGE_MAPPING_FILE INCLUDE_PROJECTS INCLUDE_TASKS EXCLUDE_COMPLETED_TASK_FILE
export SHUFFLE_TASKS SHUFFLE_SEED DATA_FREEZE_MODE
export MODE ENABLED_STAGES SUCCESS_STAGES
export MODEL REASONING_EFFORT CLAUDE_CODE_VERSION MODEL_PROVIDER AGENT PROMPT_STYLE
export CC_RUNTIME_DIR CC_INSTALL_MODE MODEL_API_KEY_ENV
export WORKERS TASK_REPEAT_N WORKER_MAX_RESTARTS RESUME_POLICY INFRA_ERROR_POLICY
export AGENT_CPUS VALIDATION_CPUS AGENT_MEMORY VALIDATION_MEMORY AGENT_PIDS_LIMIT
export VALIDATION_PIDS_LIMIT AGENT_CORE_LIMIT_BYTES
export AGENT_TIMEOUT_SECONDS AGENT_SETUP_TIMEOUT_SECONDS STAGE_SETUP_TIMEOUT_SECONDS
export STAGE_VALIDATION_TIMEOUT_SECONDS CLEANUP_BUDGET_SECONDS STOP_GRACE_SECONDS
export AGENT_API_TIMEOUT_MS AGENT_API_MAX_RETRIES AGENT_RESULT_IDLE_TIMEOUT_SECONDS
export AGENT_API_FORCE_IDLE_TIMEOUT AGENT_MAX_OUTPUT_TOKENS
export DOCKER_API_TIMEOUT_SECONDS DOCKER_TIMEOUT_RETRIES DOCKER_RETRY_DELAY_SECONDS
export DOCKER_STARTUP_CONCURRENCY DOCKER_STARTUP_LOCK_DIR
export HOST_RESERVED_CPUS HOST_RESERVED_MEMORY_GB MIN_HOST_FREE_GB
export DISK_CHECK_INTERVAL_SECONDS MAX_ATTEMPT_OUTPUT_GB MAX_CONTAINER_RW_GB
export CAPTURE_PROXY_DIR CAPTURE_PROXY_PYTHON CAPTURE_PROXY_ENTRYPOINT CAPTURE_PROXY_PORT
export CAPTURE_ROUTE_PREFIX CAPTURE_UPSTREAM_URL CAPTURE_BIND_HOST
export CAPTURE_TIMEOUT_SECONDS CAPTURE_CONNECT_TIMEOUT_SECONDS CAPTURE_READ_TIMEOUT_SECONDS
export CAPTURE_ATTEMPT_TIMEOUT_SECONDS CAPTURE_REQUEST_TIMEOUT_SECONDS CAPTURE_MAX_RETRIES
export CAPTURE_RETRY_DELAY_SECONDS CAPTURE_RETRY_MAX_DELAY_SECONDS
export CAPTURE_DRAIN_TIMEOUT_SECONDS CAPTURE_SMOKE_TIMEOUT_SECONDS
export EXPORT_SHAREGPT AUTO_FINALIZE_ON_COMPLETE ONLINE_EXPORT_ENABLED
export ONLINE_WORKERS ONLINE_INTERVAL_SECONDS STATUS_POLL_SECONDS
export SAVE_FULL_WORKSPACE IMAGE_CLEANUP_ENABLED
export RUN_SCOPE STATE_DIR LOG_DIR ATTEMPT_ROOT PID_FILE QUEUE_DB CAPTURE_LOG_ROOT
export SHARED_STATE_DIR FINALIZER_SCRIPT SHAREGPT_EXPORT_SCRIPT TRAJ_ANALYSIS_SCRIPT
