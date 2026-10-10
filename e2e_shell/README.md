# CyberGym-E2E 本地批量评测外壳

针对官方 `cybergym-e2e` 数据集（920 任务 / 139 项目）的本地批量评测控制器：
任务调度、容器生命周期、日志采集、结果管理与数据导出。具体任务提示和
阶段判定复用官方实现（`/data/cybergym-e2e/cybergym-e2e`），外壳通过显式
适配器接入，不修改官方仓库、ExploitGym 或共享 capture 代理源码。

完整功能规格见 [FUNCTIONAL_DESIGN.md](FUNCTIONAL_DESIGN.md)；
从零开始的操作教程见 [DOWNLOAD_AND_RUN.md](DOWNLOAD_AND_RUN.md)。

## 目录

```text
e2e_shell/
├── 00_config.sh                  # 全部用户参数（中文注释）+ 派生路径
├── run_all.sh                    # 自动判断新实验／活动／停止／完成
├── 01_preflight.sh               # 只读依赖/资源/端口/共存检查
├── 02_prepare_dataset.sh          # 冻结输入（新实验）或核对复用（续跑）
├── 03_start_network.sh            # scope 网络 + Squid + INPUT 规则
├── 03_start_proxy.sh             # 独立 capture（端口 30305）
├── 04_smoke_test.sh              # 最小连通性 + effort 字段证据核对
├── 05_start_evaluation.sh        # 启动单 supervisor + worker 池
├── 06_status.sh                  # 只读状态面板（--watch/--wait/--json）
├── 07_finalize_and_summarize.sh  # 离线汇总（版本目录，不覆盖）
├── 07v2.sh                       # 在线增量导出（--watch）
├── 08_stop.sh                    # 精确停止（保留全部历史，可续跑）
├── 09_reclassify.sh              # 按已有阶段证据重新分组
├── network_reverse_test.sh       # 允许/阻断路径核验
├── prepare_cc_runtime.py         # 独立准备 Node 20 + CC 2.1.91
├── orchestrator.py               # 编排入口（init/launch/supervise/status/stop/…）
├── shelllib/                     # 全部实现模块（见下表）
└── tests/                        # 114 项故障注入测试（模拟 Docker/上游）
```

| 模块 | 职责 |
|---|---|
| `config.py` | 参数解析、阶段集合约束、核心/运行指纹、秘密脱敏 |
| `manifest.py` | 输入冻结、任务筛选、ACR build_image 合并替换、续跑核对 |
| `queue.py` | SQLite WAL 任务池、短事务领取、repeat/generation、崩溃窗口恢复 |
| `supervisor.py` | 单 supervisor、worker 池、异常恢复（receipt 优先）、watchdog |
| `runner_adapter.py` | `run_attempt()`：一次独立执行的显式编排（max_attempts=1 语义） |
| `workspace.py` | 官方 setup_workspace 的可审计适配（顺序保留） |
| `validation_entry.py` | 容器内薄入口：`validate_task(only_stage=N, run_prepare=False)` 完整返回值 |
| `cc_runner.py` | CC 环境、流式日志、会话目录、终态后 idle guard、effort 证据 |
| `docker_control.py` | 共享启动槽、命名恢复、资源限制、所有权删除 |
| `image_manager.py` | digest 锁、共享 lease、owned 账本、精确清理 |
| `network.py` | scope 网络/Squid/iptables（cg-e2e-<scope> 前缀） |
| `results.py` | 阶段 raw/normalized、成功分组、result+receipt 原子提交 |
| `prompts.py` | 从官方 run_agent.py AST 提取提示（hash 校验） |
| `trajectory_adapter.py` | capture 聚合视图（去重/冲突）+ finalizer 输入布局 |
| `postprocess.py` | 07 离线汇总、完整性分析、ShareGPT |
| `online_export.py` | 07v2 在线增量导出（版本目录/原子发布/中断恢复） |
| `reclassify.py` | 09 重新分组（只读阶段证据） |
| `reports.py` | 06 状态面板与分母分离的汇总 |

## 快速开始

```bash
cd /data/cybergym-e2e/e2e_shell
# 1) 独立环境（不触碰 ExploitGym 的 arvo 环境）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 2) 准备 CC 2.1.91 共享 runtime（需要外网）
.venv/bin/python prepare_cc_runtime.py --version 2.1.91
# 3) 检查 + 计划（不启动容器、不发模型请求）
ANTHROPIC_API_KEY=... bash 01_preflight.sh && bash run_all.sh --dry-run
# 4) 正式启动（含网络反向测试与连通性检查）
bash run_all.sh --network-reverse-test
# 5) 观察 / 停止 / 接续
bash 06_status.sh --watch
bash 08_stop.sh        # 保留历史
bash run_all.sh        # 同 EXP/round 接续：已完成 repeat 跳过
```

## 关键语义

- **阶段独立执行**：所有启用阶段独立运行（前一阶段失败不跳过后续），
  每阶段全新容器；禁用阶段记 `disabled`，不填 0。
- **成功分组**：`SUCCESS_STAGES` 全部 passed 才算成功（AND，非“任一通过”）；
  必须是 `ENABLED_STAGES` 非空子集；`patch-only` 不接受 S1/S2。
- **error 分类**：官方裁判正常返回的 error 规范为 `failed/validator_error`
  （保留原始证据）；Docker 不可用、prepare 失败等才是外壳 `error`。
- **崩溃窗口**：结果先落盘（result+receipt，fsync+rename）再提交 DB；
  恢复时验证 receipt 后补记完成，不重复调用模型。
- **停止与续跑**：08 只回收本 scope 资源，保留队列/冻结输入/capture/历史；
  再跑 run_all.sh 即接续；stale supervisor（kill -9）由 PID/start/boot_id
  核对识别并恢复。

## 共存边界

- 与 ExploitGym 共用：`/tmp/cybergym-docker-startup-slots`（整机启动槽协议）、
  同一模型上游。
- 完全独立：容器/网络名前缀 `cg-e2e-`、labels `cybergym_e2e.*`、capture
  端口 30305（ExploitGym 为 30304/18667）、PID 文件、iptables comment、
  capture 目录、独立 venv 和 CC 2.1.91 runtime。
- 宿主 sysctl 只读记录，不自动修改。

## 测试

```bash
# 基础套件（stdlib 即可；fastapi 相关自动跳过）
python3 -m unittest discover -s tests -t .
# 完整套件（需要 fastapi/httpx；只读使用 arvo 环境解释器，不安装任何东西）
/root/miniconda3/envs/arvo/bin/python -m unittest discover -s tests -t .
```

覆盖：并发领取无重复、SQLite 锁竞争、stop/resume 循环、stale supervisor、
receipt 崩溃窗口、阶段独立性、官方 error 分类、Docker 超时恢复、启动槽、
上游断连/截断/重试/永久错误、session-capture 布局、在线发布中断恢复、
重新分组、scope 清理只作用自身资源。

## 故障排查

1. `bash 06_status.sh`：看 ATTENTION 原因、当前阶段、worker 状态。
2. attempt 目录：`attempts/<task>/rN/` 下 `logs/console.log`（CC 流式输出）、
   `logs/agent_exit/`（容器退出证据）、`stageN/`（验证日志与结果）、
   `result.json` / `receipt.json`。
3. capture：`capture_logs/launch_*/raw/`（completed/inflight/retry_attempts）。
4. 导出问题看 `summary/vN/`、`online/.state/last_error.json`；
   导出失败不会把已完成任务放回队列。
