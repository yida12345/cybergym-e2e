# E2E 外壳：从官方数据下载到启动与续跑

日期：2026-10-09（已按实现更新）。本文是当前外壳的实际操作教程：下载、安装、
配置、检查、启动、状态、停止、同 EXP 接续、离线／在线导出和重新分组。
完整职责与边界见 [FUNCTIONAL_DESIGN.md](FUNCTIONAL_DESIGN.md)。

所有命令默认在 `/data/cybergym-e2e/e2e_shell` 下执行。模型 key 通过环境变量
`ANTHROPIC_API_KEY` 提供，不写入 config、manifest、Git 或日志。

## 1. 官方数据访问

先登录 Hugging Face 并在 [官方数据集页面](https://huggingface.co/datasets/sunblaze-ucb/cybergym-e2e) 接受访问条件。该数据集需要获得文件访问权限，仅登录但未接受条件可能无法下载。

使用 E2E 独立 Python 环境，不向当前 ExploitGym 使用的 arvo 环境安装或升级依赖：

```bash
python3 -m venv /data/cybergym-e2e/e2e_shell/.venv
source /data/cybergym-e2e/e2e_shell/.venv/bin/activate
python -m pip install -U huggingface_hub
hf auth login
```

令牌使用自己的 HF 账号配置，不写进外壳 config、git 或任务日志。关于 CLI 安装与登录，见 [HF 官方 CLI 文档](https://huggingface.co/docs/huggingface_hub/guides/cli)。

## 2. 下载并保留断点状态

```bash
mkdir -p /data/cybergym-e2e/output/dataset
hf download sunblaze-ucb/cybergym-e2e \
  --repo-type dataset \
  --local-dir /data/cybergym-e2e/output/dataset \
  --dry-run
```

确认访问和待下载文件后执行：

```bash
hf download sunblaze-ucb/cybergym-e2e \
  --repo-type dataset \
  --local-dir /data/cybergym-e2e/output/dataset
```

网络中断后重新执行同一条命令，保留下载目录及其中 `.cache/huggingface` 元数据；已完成且版本未变的文件可以复用，不使用 force-download，也不先删除整个目录。相关行为见 [HF 的 local-dir 与 dry-run 说明](https://huggingface.co/docs/huggingface_hub/guides/cli#download-to-a-local-folder)。

按本地官方 runner 的目录约定，下载完成后数据路径应为：

```text
/data/cybergym-e2e/output/dataset/projects/<project>/<task>/src.tgz
/data/cybergym-e2e/output/dataset/projects/<project>/<task>/poc.bin
/data/cybergym-e2e/output/dataset/projects/<project>/<task>/crash.log
```

外壳的 01/02 会核对这一结构；若官方文件布局改变，需将 config 的 `DATASET_DIR` 指向实际 projects 层。项目 TOML、构建和验证脚本使用官方代码仓库（`REPO_DIR`），不把 HF 数据目录误当成脚本目录。

> 本机状态说明：2026-10-09 检查时 `/data/cybergym-e2e/output/dataset` 已
> 存在完整数据（139 个项目，约 149 GiB）。若为你首次下载，按上面命令执行。

## 3. 镜像映射准备

当前本地映射位于：

```text
/data/cybergym-e2e/acr_image_mirror/runs/default/image_mapping.json
```

本地报告 complete=true、511 个 source 镜像映射和 920 个 task 映射。外壳读取任务对应的 ACR digest（任务精确匹配优先，其次按官方 build_image 源引用回退），不必再运行官方全量 pull_images，也不需要把已推送的镜像重新构建。启动时 01 会检查映射完整性；缺失映射的任务在 02 冻结时报错。

ACR 登录态需可用（`docker login qdyd-jingxiang-registry-vpc.ap-northeast-1.cr.aliyuncs.com`）。用户名和密码通过用户自己的 Docker 登录或环境提供，不硬编码。镜像迁移工具的已有用法见 [mirror README](/data/cybergym-e2e/acr_image_mirror/README.md)。

## 4. 安装外壳依赖与准备 CC

```bash
cd /data/cybergym-e2e/e2e_shell
source .venv/bin/activate
python -m pip install -r requirements.txt
python prepare_cc_runtime.py --version 2.1.91
```

- 工具放在 `/data/cybergym-e2e/output/tools/cc-runtime`（Node 20 + 固定版本 CC），记录版本与 SHA-256；不修改、不复用 `/data/exploitgym/data/runtime`（那是 ExploitGym 的 2.1.119）。
- 运行时容器内共享只读挂载；CC 入口随版本可能是 `cli.js` 或原生 ELF，包装脚本自动判定。兼容性失败的显式备用路径是 `CC_INSTALL_MODE=per-container`。
- `prepare_cc_runtime.py` 需要外网（nodejs.org 与 registry.npmjs.org）。
- Docker SDK 控制和需要 iptables 的网络设置使用具备相应权限的运行环境；01 会检查，不能通过一部分脚本 sudo、一部分使用其他 home 的方式造成 Docker 登录态混乱。

## 5. 编辑 config

编辑 `/data/cybergym-e2e/e2e_shell/00_config.sh` 最上面的用户参数（全部
中文注释；支持环境变量临时覆盖）。建议首轮配置：

```bash
# 实验名和轮次；停止后接续时保持不变
EXP=e2e_glm53_first
ROUND_NUMBER=1

# 模式：e2e 或 patch-only
MODE=e2e
# 最终裁判启用的阶段：auto 或 "1 2 3 4"
ENABLED_STAGES=auto
# 以下阶段必须全部 passed 才分组为 successful：auto 或 "1 2 3"
SUCCESS_STAGES=auto

# 首轮共存预算
WORKERS=8
TASK_REPEAT_N=2
AGENT_CPUS=2
VALIDATION_CPUS=2
MODEL=glm53
REASONING_EFFORT=high
CLAUDE_CODE_VERSION=2.1.91
```

已确认默认值（无需再改）：Agent 内存 12g、验证 16g、Agent 超时 604800 秒、
capture 端口 30305、重试 20 次、Docker API 超时 600 秒 + 3 次重试、共享启动
槽 8 个、总 attempt 预算 675000 秒。内存、各阶段预算、retry、proxy 上游、
任务筛选（INCLUDE_PROJECTS/INCLUDE_TASKS/EXCLUDE_COMPLETED_TASK_FILE）也
在 config 顶部。

说明：

- `patch-only` 默认启用并要求 S3/S4；该模式不接受 S1/S2。
- 成功条件必须是启用阶段的非空子集；比如 e2e 只想以 S3/S4 分组，可
  `ENABLED_STAGES="1 2 3 4" SUCCESS_STAGES="3 4"`（此时为 custom profile，
  汇总中会注明）。
- 禁用阶段不会产生通过／失败证据，未来不能凭空把它加入可确定的重新分组条件。
- capture 上游和 `/glm53` 路由使用现有服务（默认 `http://8.130.156.14:8000`），
  E2E 的本地 capture 进程使用独立 30305 端口。

## 6. 首次检查和启动

```bash
cd /data/cybergym-e2e/e2e_shell
ANTHROPIC_API_KEY=<你的key> bash 01_preflight.sh
bash run_all.sh --dry-run
```

- 01 是只读检查：依赖、Docker、存储驱动（overlayfs 下可写层硬限额不可用，
  明确采用 watchdog）、数据、映射、当前实际资源预算、端口与 ExploitGym
  共存、宿主 sysctl（只读记录）。
- dry-run 只给出选中任务、阶段、计划 repeat、预算与恢复策略，不创建任务容器、不发模型请求。

正式入口：

```bash
ANTHROPIC_API_KEY=<你的key> bash run_all.sh --network-reverse-test
bash 06_status.sh
bash 06_status.sh --wait
```

run_all 依次执行：01 检查 → 02 冻结输入（或核对复用）→ 03 网络 + capture
→ （可选反向测试）→ 04 连通性检查 → 05 启动后台 supervisor。

- 04 发送一条最小模型请求（`请只回复：连接成功`），作为 **auxiliary** 请求
  在 capture 中单列，不混入 benchmark；同时从 capture 请求体核对
  REASONING_EFFORT 的实际字段与上游支持情况（`effort_evidence`），仅设置
  字符串不能作为已生效的证据。
- 反向测试验证：internal 网络容器经 Squid 访问 capture 成功、直连 capture
  与访问公网被阻断。
- 失败（检查或连通性）不会启动任务池。
- 05/06 之间可 `bash run_all.sh --no-smoke` 跳过连通性检查（不建议）。

首次验收建议用单任务清单和一个 repeat：

```bash
EXP=e2e_acceptance INCLUDE_TASKS="curl/arvo_66012" TASK_REPEAT_N=1 WORKERS=1 \
ANTHROPIC_API_KEY=<你的key> bash run_all.sh --network-reverse-test
```

验收实验与首轮实验使用不同 EXP；验收后更改 repeat 不混入已冻结实验。首轮
建立后，手动停止续跑保持同一 EXP、round 和评测参数。`run_all.sh --dry-run`
不进行模型连通性测试。

## 7. 手动停止和同 EXP 继续

```bash
bash 08_stop.sh
bash run_all.sh
```

第二条命令使用同一份 config 和同一 EXP/round。run_all 会自动判断四种状态：

1. 无队列：冻结输入、初始化新实验。
2. 合法活动 supervisor：显示状态并拒绝重复启动（PID/start/boot_id 核对）。
3. 队列 COMPLETE：直接报告已完成，不重复发模型请求。
4. STOPPED/ATTENTION/stale RUNNING：核对冻结指纹与旧资源，验证 receipt
   后补记“结果已落盘但 DB 未提交”的 attempt，恢复未完成 repeat，创建新
   launch 继续。

正常完成的 repeat 不再调用模型；停止时正在执行的 repeat 从新 generation
的新 attempt 开始，旧日志、capture 和中断记录保留。supervisor 被 kill -9
时（state 仍 RUNNING 的 stale），入口按 PID/start 识别并走同一 recover 流程。

不删除 queue.sqlite3，不改 EXP，不重置 ROUND_NUMBER，不手动删除 capture，
不自行维护 completed 排除文件。检测到未知遗留资源时会报告，不能为了接续
随意清理其他实验。`--cleanup-images` 可选：额外清理本轮 owned 且无消费者
的镜像（默认不清理）。

可以调整的运行参数（写入新 launch，不覆盖旧 manifest）：`WORKERS`、
`STATUS_POLL_SECONDS`、`ONLINE_WORKERS`、`ONLINE_INTERVAL_SECONDS`、
`STOP_GRACE_SECONDS`。改变模型、CC 版本、任务清单、阶段配置、repeat、
容器限额等核心参数需要新实验（02 会拒绝指纹不一致的续跑）。

## 8. 离线／在线采集导出

```bash
bash 06_status.sh --wait
bash 07_finalize_and_summarize.sh
```

07 聚合该 EXP/round 的所有 launch：capture 聚合视图（同 ID 同内容去重、
不同内容保留冲突）、结果适配（主 checks 只表达当前成功策略）、session ↔
capture 关联（message.id 匹配，由现有 finalizer 完成）、完整性分析
（traj_analysis 只读）、ShareGPT 导出与分母分离统计。输出写入新的版本
目录 `summary/vN/`，不覆盖旧产物；`--snapshot` 可在未完成时部分汇总。

尚在运行时可用独立在线导出器：

```bash
bash 07v2.sh --watch --interval 300
```

在线导出器中断不会停止 benchmark；结果为有版本和修正记录的 provisional
视图（`online/vN/`，原子发布 current），完成后再用 07 进行最终核对。同一
输出目录的第二个导出器实例被文件锁拒绝；发布中断由 pending 标记在下
次运行恢复。

默认 `AUTO_FINALIZE_ON_COMPLETE=true`，全部 repeat 收尾后由 supervisor 自动
执行：先按 drain 预算等待 inflight capture，再运行离线后处理；后处理失败
标记 export_status=ATTENTION，不把已完成任务重新放回队列。手动执行 07 可
核查或重试导出。

## 9. 按已有 S 结果重新分组

```bash
bash 09_reclassify.sh --success-stages "1 2 3 4"
bash 09_reclassify.sh --success-stages "3 4"
```

只读已有阶段证据（stage_results 表），不调用模型或验证，不覆盖原始 queue
／评分。输出到独立 policy 目录 `regroup/<policy_hash>/`，同时列出不可评估
记录和覆盖率。对 disabled 阶段没有可恢复的通过信息，需要新的评测才能获得；
引用未执行阶段时结果为 not_evaluable，不伪造失败。

## 10. 共存和故障定位

新外壳的容器／网络前缀为 `cg-e2e-`（labels `cybergym_e2e.*`），capture 默认
30305；现有 ExploitGym 的 -exp 网络、30304、18667、工具目录和 tmux exprun
保持独立。共享部分只有明确约定的整机 Docker 启动信号量
（`/tmp/cybergym-docker-startup-slots`）以及同一个模型上游。01 只读记录宿主
sysctl，不会自动修改正在运行的其他评测依赖的全局设置。

先看 `06_status.sh` 的当前阶段和 ATTENTION 原因，再看对应 attempt 的
`logs/console.log`（CC 流式输出）、`stageN/`（验证日志）、`result.json`、
`receipt.json`，capture 的 `raw/completed|inflight|retry_attempts`，以及
queue 状态。无 summary、失败退出码、模型重试、Docker timeout、benchmark
failed 和手动 interrupted 是不同口径，不能互相代替：
`06_status.sh --json` 中 outcomes 分别列出。

停止只删除本 scope 的规则、网络、服务和容器（按 labels 核对所有权），
不使用 pkill/全局 prune，不删除共用的 Docker 启动锁目录，也不停止
tmux exprun。

## 11. 真实验收缺项（本次会话未执行）

本会话在无外网沙箱中完成代码、模拟测试与 dry-run，以下需要联网或有凭据
时执行，当前**未**执行、也**未**验证：

1. `.venv` 与 requirements.txt 安装（无 PyPI 访问；当前测试用宿主 Python
   与只读 arvo 解释器完成）。
2. `prepare_cc_runtime.py` 实际下载 Node 20 与 `@anthropic-ai/claude-code@2.1.91`。
3. 04 连通性检查（真实模型请求）与 effort 字段上游证据。
4. 单任务真实验收（一个任务 × 一个 repeat 的正式评测）。
5. 正式批量评测与 07/07v2 对真实产物的导出。
