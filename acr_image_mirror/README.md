# CyberGym-E2E 官方镜像并行归档到 ACR

本目录读取 `/data/cybergym-e2e/cybergym-e2e/projects` 的 `project.toml` 和任务
`config.toml`，按官方 `scripts/pull_images.py` 的规则收集、去重 `build_image`，
将镜像拉取、重标并推送到 ACR。只搬运镜像，不进行 ExploitGym 转换或重新编译。
不修改官方仓库和原始配置。

参考 `/data/ztr/arvo3_to_cybergym/cybergym_to_ExploitGym/convert.py` 的有界并发、
独立 push 并发、重试、状态落盘和发布校验流程，使用独立实现，避免依赖转换项目的
Python 环境。需要 Linux、Python 3.11+ 和支持 `docker push --platform` 的 Docker；
Python 3.10 可安装 `tomli`。
默认 `--copy-backend auto`：先使用 Docker，遇到本地平台内容不完整时使用 `skopeo` 直接复制。
该恢复路径需要安装 `skopeo`；可用 `--copy-backend skopeo` 始终直接复制，或 `docker` 禁用恢复路径。
不需要 Hugging Face token，也不会下载源码数据集。

## 默认配置

| 配置 | 默认值 |
| --- | --- |
| 官方仓库 | `/data/cybergym-e2e/cybergym-e2e` |
| ACR registry | `qdyd-jingxiang-registry-vpc.ap-northeast-1.cr.aliyuncs.com` |
| namespace / repository | `qdyd / cybergym-e2e` |
| 镜像平台 | `linux/amd64` |
| 在途镜像数 `--workers` | 4 |
| 推送并发 `--push-workers` | 2 |
| 每条 Docker / skopeo 命令最大尝试次数 | 3 |
| 初始重试等待 | 10 秒，随后指数退避 |
| pull / push 超时 | 7200 秒 |
| manifest / inspect / tag / rm 超时 | 120 秒 |
| 结果目录 | 本目录 `runs/default/` |

2026-10-08 的本地官方仓库包含 139 个项目、920 个任务、511 个唯一镜像：
`n132/arvo` 376 个、`cybergym/oss-fuzz` 99 个、`cybergym/e2e` 33 个、GCR base-builder 3 个。
实际数量每次从当前配置重新计算。默认收集全部配置；`--tasks-file` 可以只选官方任务列表。
平台参数只镜像一个平台，不复制整个多架构 manifest list。

## 使用

先生成完整清单，不访问 Docker 或网络：

```bash
bash /data/cybergym-e2e/acr_image_mirror/run.sh --dry-run
```

确认 ACR 中已创建 `qdyd/cybergym-e2e` repository，当前宿主机能访问其 VPC 地址。
使用有 push 权限的账号交互登录；本工具不保存或硬编码用户名、密码：

```bash
docker login \
  qdyd-jingxiang-registry-vpc.ap-northeast-1.cr.aliyuncs.com \
  --username '<你的 ACR 用户名>'
```

先迁移一个唯一镜像，再跑完整清单；第二次会校验并跳过已完成镜像：

```bash
bash /data/cybergym-e2e/acr_image_mirror/run.sh --limit 1
bash /data/cybergym-e2e/acr_image_mirror/run.sh --workers 4 --push-workers 2
```

自定义目标仓库：

```bash
bash /data/cybergym-e2e/acr_image_mirror/run.sh \
  --acr-registry '<你的 registry 域名>' \
  --acr-namespace qdyd \
  --acr-repository cybergym-e2e \
  --workers 4 --push-workers 2
```

也可用 `ACR_REGISTRY`、`ACR_NAMESPACE`、`ACR_REPOSITORY` 环境变量覆盖默认目标；
`MIRROR_PYTHON` 用来指定 Python 可执行文件。

指定任务子集或自定义输出目录：

```bash
bash /data/cybergym-e2e/acr_image_mirror/run.sh \
  --tasks-file /data/cybergym-e2e/cybergym-e2e/scripts/tasks.txt \
  --output-dir /data/cybergym-e2e/acr_image_mirror/runs/subset
```

`--limit N` 按源引用排序后只处理前 N 个唯一镜像，不是前 N 个任务。
完整选项：`bash /data/cybergym-e2e/acr_image_mirror/run.sh --help`。

## Docker 拉取成功但推送报平台错误

containerd 本地存储可能保留某层的解压快照，却没有对应的原始压缩 blob。
受 Docker/containerd 缓存复用缺陷影响，重新 pull 可能跳过该 blob，仍报告成功。
`docker push --platform` 则可能报 `was found but does not provide the specified platform`。
工具现在识别这个错误，不再重复重试同一条推送；默认自动切换到 `skopeo copy`。

复制使用已选平台的 `源仓库@manifest-digest`，通过 `docker://` 直接复制到 ACR，
绕过 Docker daemon，不创建本地 Docker 镜像，也不保存完整镜像归档。
`--preserve-digests` 保留源内容，复制完成再校验复制结果、ACR manifest 和 config digest。
这条路径同样受推送并发、超时、重试和限额停止约束，成功、失败或正常中断仍清理任务本地引用。
临时 digest 文件随任务收尾删除。失败任务记录复制方式，后续默认续跑直接使用 `skopeo`。

当前两个 GCR base-builder 失败镜像可以这样续跑，已完成镜像仍只核对 ACR：

```bash
bash /data/cybergym-e2e/acr_image_mirror/run.sh --copy-backend skopeo
```

默认通过 `--authfile` 使用当前用户的 Docker `config.json`，尊重 `DOCKER_CONFIG`，
沿用 `docker login` 的登录信息；凭据不会写入命令参数或日志。
如果 Docker 配置不存在，则使用 `skopeo` 的默认凭据查找规则。
可用 `--skopeo-authfile PATH` 指定凭据文件，或 `--skopeo PATH` 指定可执行文件。
如果宿主机没有 `containers/policy.json`，自动为当前复制创建临时策略：默认拒绝，
仅允许已选定的完整源镜像 digest，复制后删除。沿用 Docker 的无签名镜像使用方式，
HTTPS、digest 和内容校验仍然启用。已有宿主机策略不会覆盖，可用 `--skopeo-policy PATH`
明确选择策略文件；已有策略的拒绝错误不会自动放行。
本工具不修改 Docker 服务配置或删除共享快照。

## 推送与续跑

每个镜像的处理流程：

续跑时，如果本地状态记录了已验证的源和目标 digest，先只查询 ACR。
ACR manifest/config digest 与记录一致就跳过，不查询源仓库、不下载镜像层。
ACR 内容改变则报错；ACR 镜像不存在或没有完整验证记录时，执行下面的完整流程：

1. 查询源 manifest，选择目标平台并获得 manifest digest 和 config digest。
2. 查询 ACR tag：明确不存在才进入发布；内容与源一致则跳过 layer 下载和推送；
   内容不同、鉴权失败或网络错误不会当成“已完成”或“目标不存在”。
3. 从 `源仓库@manifest-digest` 拉取，避免更新宿主机已有的源 tag。
4. 添加 ACR tag，通过独立 semaphore 限制 push 并发。
5. 推送后重新查询 ACR，对比源/目标 config digest，并核对 push 输出里的 manifest digest。
   config digest 约束镜像配置和 rootfs 内容；源/目标 manifest digest 不要求相同，
   因为 registry/媒体格式变化可能改变 manifest 表示。
6. 每个任务在 `finally` 中清理任务相关本地引用；成功、跳过、失败和正常中断均执行。

目标 tag 使用“可读源引用 + 完整源引用及平台的 SHA256 前 16 位”，支持带 digest、
带 registry 端口以及很长的源镜像名，并对清单内 tag 碰撞进行检查，例如：

```text
cybergym/e2e:arrow
  -> .../qdyd/cybergym-e2e:cybergym-e2e-arrow-e99979620b2d5969
```

保留原 `--output-dir`（默认 `runs/default`），同样的参数再次运行即续跑。
默认复用上次已验证的源快照，因此不会主动检查源可变 tag 是否更新。
需要重新检查上游内容时添加 `--refresh-source`；这会再次访问源仓库并消耗其请求额度。
若源的可变 tag 内容更新，已存在的 ACR tag 会产生冲突；用 `--tag-prefix v2-`
迁移到新标签，不自动覆盖旧镜像。删除状态目录后将无法使用仅查询 ACR 的续跑路径。

遇到 `toomanyrequests` / pull rate limit 时立即停止派发新任务，不进行短间隔重试。
在途任务停止后仍执行本地镜像清理，并保存进度；`report.json` 会记录
`rate_limited=true` 和 `stop_reason="rate_limit"`，退出码为 1。
登录源仓库或等待额度恢复后重新运行原命令即可；脚本不会自动等待配额恢复。
修改脚本后，已经运行的旧进程需要先正常停止，再重新启动才能应用新行为。

`--workers` 限制从查询到清理的整个在途镜像数，因此等待 push 的镜像也占用槽位，
不会无限积压本地大镜像。任一镜像完成即补位。源引用锁和实际 digest 引用锁跨输出目录
共享，避免两个源 tag 指向同一 digest 时互相清理；锁只覆盖本机，不协调其他宿主机。
多个副本应使用相同的 `--lock-dir`；默认在 `/tmp/cybergym-e2e-acr-locks`。

任务结束后始终清理账本中的源镜像和 ACR 本地引用，也清理该任务预先存在的同内容引用。
已移除 `--keep-local-images` 选项。推送重试耗尽、pull 失败、校验失败、跳过已完成镜像、
Ctrl+C / SIGTERM 等正常收尾路径都会执行清理，不再为诊断刻意保留镜像。
清理仍核对引用内容，不会删除无关镜像、远端镜像，也不会执行全局 `prune`。
只要清理成功，普通失败任务记录错误后可以继续其他任务；限额错误会停止整个批次。
续跑失败任务会重新拉取必要镜像。
旧版本留下的引用也会在原输出目录续跑时处理。

清理命令同样有重试和超时。镜像正在被容器使用、Docker 不可用、引用被其他程序改变等
导致删除失败时，报告会标记 `cleanup_complete=false`，保留未删引用账本并停止派发新任务。
镜像层只有在不被其他引用或容器使用时才由 Docker 回收。
清理在源引用和 digest 引用锁释放前执行，避免并发任务互相清理。

Ctrl+C / SIGTERM 会停止新任务和当前 Docker CLI，随后清理并写出结果。
SIGKILL、主机掉电无法执行 Python `finally`；Docker daemon 已接收的 layer 操作也可能
继续执行，不能承诺这些异常下完全无残留。保留输出状态，恢复后原命令续跑处理。
退出码：全部完成为 0，有失败/未完成为 1，中断为 130。

## 输出

```text
runs/default/
├── plan.json            # 全部源/目标映射及使用这些镜像的配置路径
├── report.json          # 完成数、失败信息、待处理数及日志路径
├── image_mapping.json   # 通过验证的源引用 -> ACR digest 引用，以及任务映射
├── state/*.json         # 每个镜像的已验证 digest 和本地引用所有权
└── logs/*.log           # Docker 完整输出，续跑追加
```

`image_mapping.json` 的 `complete` 标记表示所选镜像是否全部成功；`source_to_acr`
仅包含本次通过验证的镜像。单个平台有多份匹配 manifest、缺少 config digest 等
无法确认内容的情况会报错。详细 Docker 输出写日志，终端显示阶段、完成数量和重试。

归档成功后，官方评测仍读取原来的 `build_image`，不会自动切换到 ACR。
后续可使用 `source_to_acr` 映射生成一份独立的 ACR 项目配置，或从 ACR 拉取后
添加官方兼容引用；本工具没有修改原始 `projects`。

## 镜像大小统计

统计已归档镜像的压缩大小（完全离线读取已有 manifest 日志，不下载镜像）：

```bash
python3 /data/cybergym-e2e/acr_image_mirror/image_sizes.py
```

结果保存在 `runs/default/size_stats/`：`summary.json` 为汇总，`images.csv` 为按大小排序的
逐镜像明细，`report.md` 为可阅读报告。分别计算逐镜像相加和所有 layer/config/manifest
按 digest 去重的数据量。原始 manifest 字节按 ACR digest 校验；若日志缺失则报错，
不会将缺失镜像按零计入。可用 `--run-dir` 指定其他归档目录。
该结果不代表解压后的本地磁盘占用，也不包含 ACR 仓库额外开销或清单外其他内容。

## 验证

```bash
python3 -m unittest discover \
  -s /data/cybergym-e2e/acr_image_mirror/tests -v
```

测试覆盖配置收集、平台选择、tag 去重、远端冲突、失败后续跑、失败/中断清理、清理任务已有引用、
共享 digest 的并发清理、pull/push 并发上限、命令重试与取消、dry-run 无网络行为。
另覆盖只查询 ACR 的续跑、显式刷新源、ACR 缺失修复，以及限额时停止派发和清理。
覆盖平台内容错误立即切换直接复制、禁用恢复路径、直接复制失败/中断/限额后的清理、
复制摘要验证、凭据文件传递和复制方式续跑。
使用模拟 Docker 和本地子进程，不会访问真实 registry 或模型服务。
