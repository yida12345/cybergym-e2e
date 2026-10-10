#!/usr/bin/env python3
"""CyberGym-E2E 外壳编排入口：init/resume/supervise/worker/status/stop 等。

调度逻辑不散落在 shell 中；所有子命令从环境（00_config.sh export）读取
配置。子命令：

- check-new / status：只读检查与状态
- init：冻结输入并初始化队列（新实验）
- resume：同 EXP 接续（stop/stale/attention 恢复）
- launch：启动后台 supervisor（单实例，scope 启动锁保护）
- supervise：supervisor 主循环（由 launch 派生，不直接调用）
- stop：请求停止（08 使用）
- recover：stale supervisor / 崩溃窗口恢复
- cleanup-scope：精确回收本 scope 容器、网络与 iptables 规则
- dry-run：只打印计划，不创建任务容器、不发模型请求
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SHELLLIB_DIR = Path(__file__).resolve().parent
if str(SHELLLIB_DIR) not in sys.path:
    sys.path.insert(0, str(SHELLLIB_DIR))

from shelllib import util  # noqa: E402
from shelllib.config import (  # noqa: E402
    ConfigError,
    ShellConfig,
    load_config_from_env,
    total_attempt_budget_seconds,
)
from shelllib.queue import TaskPool  # noqa: E402


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _launch_id(cfg: ShellConfig) -> str:
    return f"launch-{time.strftime('%Y%m%d-%H%M%S')}-{util.scope_hash(cfg.exp, str(cfg.round_number), str(time.time()))[:6]}"


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_check_new(_args) -> int:
    cfg = load_config_from_env()
    if cfg.queue_db_path().exists():
        return _fail(f"队列已存在: {cfg.queue_db_path()}；同 EXP 续跑直接运行 run_all.sh")
    return 0


def cmd_init(args) -> int:
    cfg = load_config_from_env()
    if cfg.queue_db_path().exists():
        return _fail(f"队列已存在: {cfg.queue_db_path()}；不重复初始化")
    from shelllib import manifest as manifest_mod

    report = manifest_mod.prepare_new_experiment(cfg)
    pool = TaskPool(cfg.queue_db_path())
    tasks = [task["task_path"] for task in report["tasks"]]
    pool.initialize(tasks, cfg.stage_policy.mode, cfg.task_repeat_n, cfg.workers)
    print(json.dumps({
        "initialized": True,
        "scope": cfg.scope,
        "tasks": len(tasks),
        "repeat_n": cfg.task_repeat_n,
        "manifest": str(cfg.manifest_path()),
        "dry_run": bool(args.dry_run),
    }, ensure_ascii=False))
    return 0


def cmd_resume(args) -> int:
    """同 EXP 接续：核对指纹、恢复未完成 repeat、清除停止标志。"""
    cfg = load_config_from_env()
    if not cfg.queue_db_path().exists():
        return _fail(f"队列不存在: {cfg.queue_db_path()}；先运行 init 或 run_all.sh")
    from shelllib import manifest as manifest_mod

    verification = manifest_mod.verify_manifest_for_resume(cfg)
    pool = TaskPool(cfg.queue_db_path())

    # 崩溃窗口恢复：结果已落盘、DB 未提交的 attempt 补记完成
    receipt_stats = pool.recover_finished_from_receipts(
        verify=lambda attempt: (
            __import__("shelllib.results", fromlist=["verify_receipt"]).verify_receipt(
                Path(attempt["output_dir"]), cfg.stage_policy.enabled_stages
            )
            is not None
        ),
        required_stages=cfg.stage_policy.enabled_stages,
    )

    # stop/stale 残留归位
    reset = pool.recover_interrupted_pool()

    # DB 已完成但结果缺失 → 明确报告异常（不静默跳过）
    missing = pool.verify_done_results_exist()

    if pool.is_complete() and not missing:
        print(json.dumps({"state": "COMPLETE", "verified": verification}, ensure_ascii=False))
        return 0

    pool.clear_stop()
    print(json.dumps({
        "state": "RESUMED",
        "verified": verification,
        "receipts_recovered": receipt_stats["recovered"],
        "recovered": {**reset, **receipt_stats},
        "missing_results": missing,
    }, ensure_ascii=False))
    if missing and args.strict:
        return 1
    return 0


def cmd_launch(args) -> int:
    """启动后台 supervisor；scope 启动锁保护，单实例。"""
    cfg = load_config_from_env()
    cfg.derive()
    lock = util.FileLock(cfg.state_dir / "launch.lock")
    if not lock.acquire(blocking=False):
        return _fail("另一个启动流程正在进行（launch.lock）")
    try:
        # 1) 无队列：初始化
        if not cfg.queue_db_path().exists():
            from shelllib import manifest as manifest_mod

            manifest_mod.prepare_new_experiment(cfg)
            pool = TaskPool(cfg.queue_db_path())
            tasks = [t["task_path"] for t in util.read_json(cfg.manifest_path())["tasks"]]
            pool.initialize(tasks, cfg.stage_policy.mode, cfg.task_repeat_n, cfg.workers)
        else:
            # 2) 合法活动 supervisor：拒绝重复启动
            from shelllib.supervisor import supervisor_alive

            alive, _reason = supervisor_alive(cfg)
            if alive:
                status = cmd_status_payload(cfg)
                print(json.dumps({"state": "ACTIVE", "status": status}, ensure_ascii=False, indent=2))
                return _fail("合法 supervisor 仍在运行，拒绝重复启动")
            # 3) COMPLETE：直接报告
            pool = TaskPool(cfg.queue_db_path())
            if pool.is_complete() and not pool.verify_done_results_exist():
                print(json.dumps({"state": "COMPLETE"}, ensure_ascii=False))
                return 0
            # 4) STOPPED/ATTENTION/stale RUNNING：恢复
            _resume_into(cfg)

        launch_id = _launch_id(cfg)
        # capture 目录由 03_start_proxy 按 proxy launch 建立（services.json 记录）；
        # supervisor 的 network_info 使用该目录，不在此另建空目录。

        # 网络与 capture 已由 03 系列脚本就绪；supervisor 只做调度
        network_info = _read_services(cfg)
        if args.dry_run:
            print(json.dumps({
                "dry_run": True, "launch_id": launch_id, "scope": cfg.scope,
                "workers": cfg.workers, "repeat_n": cfg.task_repeat_n,
                "total_attempt_budget_seconds": total_attempt_budget_seconds(cfg),
                "stage_policy": cfg.stage_policy.to_manifest(),
            }, ensure_ascii=False, indent=2))
            return 0

        log_path = cfg.log_dir / "supervisor.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab", 0)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "supervise",
             "--launch-id", launch_id],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "CG_E2E_LAUNCH_ID": launch_id,
                 "CG_E2E_NETWORK_INFO": json.dumps(network_info)},
        )
        _ = process
        util.atomic_write_json(cfg.state_dir / "launch_started.json", {
            "launch_id": launch_id, "supervisor_pid": process.pid,
            "started_at": util.utc_now_iso(),
        })
        print(json.dumps({
            "launched": True, "launch_id": launch_id,
            "supervisor_pid": process.pid,
            "status_command": f"bash {SHELLLIB_DIR / '06_status.sh'}",
            "stop_command": f"bash {SHELLLIB_DIR / '08_stop.sh'}",
        }, ensure_ascii=False))
        return 0
    finally:
        lock.release()


def _resume_into(cfg: ShellConfig) -> None:
    from shelllib import manifest as manifest_mod
    from shelllib.results import verify_receipt

    manifest_mod.verify_manifest_for_resume(cfg)
    pool = TaskPool(cfg.queue_db_path())
    pool.recover_finished_from_receipts(
        verify=lambda attempt: verify_receipt(
            Path(attempt["output_dir"]), cfg.stage_policy.enabled_stages
        ) is not None,
        required_stages=cfg.stage_policy.enabled_stages,
    )
    pool.recover_interrupted_pool()
    pool.clear_stop()


def _read_services(cfg: ShellConfig) -> dict:
    """读取 03 系列脚本写入的服务信息。"""
    services_path = cfg.state_dir / "services.json"
    if services_path.exists():
        return util.read_json(services_path)
    raise SystemExit(f"ERROR: 服务信息不存在，请先运行 03 系列脚本: {services_path}")


def cmd_supervise(args) -> int:
    cfg = load_config_from_env()
    launch_id = args.launch_id or os.environ.get("CG_E2E_LAUNCH_ID")
    if not launch_id:
        return _fail("supervise 需要 --launch-id")
    network_info = json.loads(os.environ.get("CG_E2E_NETWORK_INFO", "{}"))
    from shelllib.supervisor import supervisor_main

    supervisor_main(cfg, launch_id, network_info)
    return 0


def cmd_status_payload(cfg: ShellConfig) -> dict:
    from shelllib.reports import status_payload

    return status_payload(cfg)


def cmd_status(args) -> int:
    cfg = load_config_from_env()
    payload = cmd_status_payload(cfg)
    from shelllib.reports import render_status_text

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_status_text(payload))
    if args.wait:
        deadline = time.monotonic() + (args.wait_timeout or 86400 * 30)
        pool = TaskPool(cfg.queue_db_path())
        while time.monotonic() < deadline:
            state = (payload.get("meta") or {}).get("controller_state")
            if state in ("complete", "stopped", "attention"):
                break
            time.sleep(args.poll or cfg.status_poll_seconds)
            payload = cmd_status_payload(cfg)
            if not args.json:
                print(render_status_text(payload))
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.watch and not args.wait:
        while True:
            time.sleep(args.poll or cfg.status_poll_seconds)
            payload = cmd_status_payload(cfg)
            print("\x1b[2J\x1b[H" if not args.json else "")
            print(render_status_text(payload))
    return 0


def cmd_stop(args) -> int:
    """08 的核心：停止领取、中断活动 attempt、回收本 scope 资源。

    顺序：写 stop_requested → 等 supervisor 退出（超时按 PID/start 精确
    终止）→ 回收 scope 容器/网络/iptables → 停止本 scope capture 代理 →
    （可选）清理 owned 镜像。历史数据（队列/冻结输入/capture/结果）保留。
    """
    cfg = load_config_from_env()
    cfg.derive()
    lock = util.FileLock(cfg.state_dir / "stop.lock")
    if not lock.acquire(blocking=False):
        return _fail("另一个停止流程正在进行")
    try:
        pool = TaskPool(cfg.queue_db_path()) if cfg.queue_db_path().exists() else None
        if pool is not None:
            pool.request_stop()

        # 等待 supervisor 自行收尾
        from shelllib.supervisor import read_pid_file, supervisor_alive

        payload = read_pid_file(cfg)
        if payload:
            deadline = time.monotonic() + cfg.stop_grace_seconds
            while time.monotonic() < deadline:
                alive, _ = supervisor_alive(cfg)
                if not alive:
                    break
                time.sleep(1)
            alive, _reason = supervisor_alive(cfg)
            if alive:
                pid = payload.get("pid")
                if isinstance(pid, int):
                    util.terminate_process_tree(pid, grace_seconds=10.0)

        # scope 资源精确回收（容器、网络、iptables）
        report = cmd_cleanup_scope_resources(cfg, remove_images=bool(args.cleanup_images))

        # 停止本 scope capture 代理（核对 PID/start/命令行与所有权）
        proxy_report = cmd_proxy_stop(args)
        if proxy_report != 0:
            report.setdefault("errors", []).append("capture proxy 停止失败，见 proxy-stop 输出")

        state = pool.controller_state() if pool is not None else "no-queue"
        print(json.dumps({"stopped": True, "controller_state": state,
                          "cleanup": report}, ensure_ascii=False, indent=2))
        return 0
    finally:
        lock.release()


def cmd_cleanup_scope_resources(cfg: ShellConfig, *, remove_images: bool) -> dict:
    """精确回收：本 scope 容器、网络、iptables 规则；不动历史数据。"""
    from shelllib.docker_control import DockerControl
    from shelllib.network import NetworkNames

    report: dict = {"containers_removed": 0, "networks": {}, "errors": []}
    docker = DockerControl(
        api_timeout=cfg.docker_api_timeout_seconds,
        timeout_retries=cfg.docker_timeout_retries,
        retry_delay_seconds=cfg.docker_retry_delay_seconds,
        startup_lock_dir=cfg.docker_startup_lock_dir,
        startup_concurrency=cfg.docker_startup_concurrency,
    )
    # 只删除本外壳 labels 的容器
    containers = docker.list_scope_containers(cfg.scope)
    for container in containers:
        try:
            docker.remove_container(container, verify_labels={"cybergym_e2e.scope": cfg.scope})
            report["containers_removed"] += 1
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"container {container}: {exc}")
    names = NetworkNames(cfg.scope)
    from shelllib.network import NetworkManager

    manager = NetworkManager(docker, names)
    report["networks"] = manager.down()
    if remove_images:
        from shelllib.image_manager import ImageManager, load_task_image_map

        images = ImageManager(
            docker, cfg.output_root / "shared_state",
            cfg.run_scope / "state" / "pulled_images.jsonl",
            cleanup_enabled=True,
        )
        manifest = util.read_json(cfg.manifest_path()) if cfg.manifest_path().exists() else {}
        task_map = load_task_image_map(manifest)
        finished = {}
        if cfg.queue_db_path().exists():
            pool = TaskPool(cfg.queue_db_path())
            counts = pool.counts()
            for task in counts["tasks"]:
                finished[task] = counts["tasks"][task] == "done"
        report["images"] = images.cleanup_owned_images(
            scope=cfg.scope, consumer_tasks_finished=finished, task_image_map=task_map
        )
    return report


def cmd_cleanup(args) -> int:
    cfg = load_config_from_env()
    report = cmd_cleanup_scope_resources(cfg, remove_images=bool(args.cleanup_images))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_recover(args) -> int:
    """stale supervisor / 崩溃窗口恢复；受 scope 启动锁保护。"""
    cfg = load_config_from_env()
    if not cfg.queue_db_path().exists():
        return _fail("队列不存在")
    lock = util.FileLock(cfg.state_dir / "launch.lock")
    if not lock.acquire(blocking=False):
        return _fail("恢复期间检测到并发的启动流程")
    try:
        _resume_into(cfg)
        payload = cmd_status_payload(cfg)
        print(json.dumps({"recovered": True, "status": payload}, ensure_ascii=False, indent=2))
        return 0
    finally:
        lock.release()


def cmd_preflight_report(args) -> int:
    """dry-run 使用的只读计划报告。"""
    cfg = load_config_from_env()
    from shelllib import manifest as manifest_mod

    payload = {
        "scope": cfg.scope,
        "exp": cfg.exp,
        "round_number": cfg.round_number,
        "mode": cfg.stage_policy.mode,
        "stage_policy": cfg.stage_policy.to_manifest(),
        "workers": cfg.workers,
        "task_repeat_n": cfg.task_repeat_n,
        "model": cfg.model,
        "reasoning_effort": cfg.reasoning_effort,
        "claude_code_version": cfg.claude_code_version,
        "total_attempt_budget_seconds": total_attempt_budget_seconds(cfg),
        "capture_port": cfg.capture_proxy_port,
    }
    try:
        tasks, dropped = manifest_mod.select_tasks(cfg)
        payload["selected_tasks"] = len(tasks)
        payload["task_selection_dropped"] = dropped
        payload["first_tasks"] = tasks[:10]
    except manifest_mod.FreezeError as exc:
        payload["task_selection_error"] = str(exc)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 服务控制：网络、capture 代理、连通性与反向测试
# ---------------------------------------------------------------------------


def _docker_control(cfg: ShellConfig):
    from shelllib.docker_control import DockerControl

    return DockerControl(
        api_timeout=cfg.docker_api_timeout_seconds,
        timeout_retries=cfg.docker_timeout_retries,
        retry_delay_seconds=cfg.docker_retry_delay_seconds,
        startup_lock_dir=cfg.docker_startup_lock_dir,
        startup_concurrency=cfg.docker_startup_concurrency,
    )


def cmd_network_up(_args) -> int:
    """03_start_network：创建 scope 网络、Squid 与 INPUT 规则。"""
    cfg = load_config_from_env()
    cfg.derive()
    from shelllib.network import NetworkManager, NetworkNames, port_in_use

    if port_in_use(cfg.capture_bind_host, cfg.capture_proxy_port):
        known = cfg.state_dir / "services.json"
        if not known.exists():
            return _fail(
                f"端口 {cfg.capture_proxy_port} 已被占用且不是本 scope 的 capture；"
                "请检查占用进程或更换 CAPTURE_PROXY_PORT"
            )
    docker = _docker_control(cfg)
    names = NetworkNames(cfg.scope)
    manager = NetworkManager(docker, names)
    services = manager.up(
        capture_host=cfg.capture_bind_host or "127.0.0.1",
        capture_port=cfg.capture_proxy_port,
    )
    bridge = manager.block_internal_bridge_to_host()
    util.atomic_write_json(cfg.state_dir / "network.json", {
        "scope": cfg.scope, "bridge": bridge, **services,
        "rule_comment": names.rule_comment,
    })
    print(json.dumps({"network_up": True, "scope": cfg.scope, **services,
                      "blocked_bridge": bridge}, ensure_ascii=False, indent=2))
    return 0


def cmd_network_down(_args) -> int:
    cfg = load_config_from_env()
    docker = _docker_control(cfg)
    from shelllib.network import NetworkManager, NetworkNames

    report = NetworkManager(docker, NetworkNames(cfg.scope)).down()
    with contextlib.suppress(OSError):
        (cfg.state_dir / "network.json").unlink()
    print(json.dumps({"network_down": True, "report": report}, ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 1


def _proxy_health(cfg: ShellConfig, host: str) -> dict | None:
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://{host}:{cfg.capture_proxy_port}/healthz", timeout=5
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _verify_capture_policy(cfg: ShellConfig, health: dict) -> list[str]:
    """健康检查核对实际重试参数、完整缓存、上游与日志路径。"""
    problems = []
    expected = {
        "timeout_seconds": cfg.capture_timeout_seconds,
        "connect_timeout_seconds": cfg.capture_connect_timeout_seconds,
        "read_timeout_seconds": cfg.capture_read_timeout_seconds,
        "attempt_timeout_seconds": cfg.capture_attempt_timeout_seconds,
        "request_timeout_seconds": cfg.capture_request_timeout_seconds,
        "max_retries": cfg.capture_max_retries,
        "retry_delay_seconds": cfg.capture_retry_delay_seconds,
        "retry_retry_max_delay_seconds": cfg.capture_retry_max_delay_seconds,
        "retry_max_delay_seconds": cfg.capture_retry_max_delay_seconds,
    }
    policy = health.get("retry_policy", {})
    for name, value in expected.items():
        if name in policy and policy[name] != value:
            problems.append(f"{name}: {policy.get(name)} != {value}")
    if health.get("buffer_response") is not True:
        problems.append("buffer_response is not True")
    if health.get("upstream_url", "").rstrip("/") != cfg.capture_upstream_url:
        problems.append(f"upstream: {health.get('upstream_url')}")
    return problems


def cmd_proxy_up(_args) -> int:
    """03_start_proxy：为 launch 启动独立 capture；健康核对。"""
    cfg = load_config_from_env()
    cfg.derive()
    from shelllib.network import NetworkManager, NetworkNames

    docker = _docker_control(cfg)
    names = NetworkNames(cfg.scope)
    manager = NetworkManager(docker, names)
    if not (cfg.state_dir / "network.json").exists():
        return _fail("网络未就绪；先运行 03_start_network.sh")
    gateway = manager.network_gateway(names.services)
    bind_host = cfg.capture_bind_host or gateway

    launch_id = os.environ.get("CG_E2E_LAUNCH_ID") or _launch_id(cfg)
    capture_dir = cfg.run_scope / "capture_logs" / launch_id
    capture_dir.mkdir(parents=True, exist_ok=True)

    health = _proxy_health(cfg, bind_host)
    if health is not None:
        if str(health.get("capture_root", "")) != str(capture_dir / "raw"):
            return _fail(f"端口 {cfg.capture_proxy_port} 上的代理属于其他 run: {health}")
        problems = _verify_capture_policy(cfg, health)
        if problems:
            return _fail("capture 代理策略不匹配，需重启: " + "; ".join(problems))
        # 已有本 run 的健康代理：复用（幂等）
        _write_services(cfg, launch_id, bind_host, gateway, manager)
        print(json.dumps({"proxy": "reused", "launch_id": launch_id}, ensure_ascii=False))
        return 0

    # 端口占用但健康检查失败：可能是未知进程，报错不猜测。
    from shelllib.network import port_in_use

    if port_in_use(bind_host, cfg.capture_proxy_port):
        return _fail(f"端口 {cfg.capture_proxy_port} 被未知进程占用（健康检查失败）")

    command = [
        cfg.capture_proxy_python, str(cfg.capture_proxy_entrypoint),
        "--listen-host", bind_host,
        "--listen-port", str(cfg.capture_proxy_port),
        "--upstream-url", cfg.capture_upstream_url,
        "--log-dir", str(capture_dir),
        "--timeout-seconds", str(int(cfg.capture_timeout_seconds)),
        "--connect-timeout-seconds", str(int(cfg.capture_connect_timeout_seconds)),
        "--read-timeout-seconds", str(int(cfg.capture_read_timeout_seconds)),
        "--attempt-timeout-seconds", str(int(cfg.capture_attempt_timeout_seconds)),
        "--request-timeout-seconds", str(int(cfg.capture_request_timeout_seconds)),
        "--max-retries", str(cfg.capture_max_retries),
        "--retry-delay-seconds", str(int(cfg.capture_retry_delay_seconds)),
        "--retry-max-delay-seconds", str(int(cfg.capture_retry_max_delay_seconds)),
    ]
    log_path = cfg.log_dir / f"capture_proxy_{launch_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_file = cfg.state_dir / "capture_proxy.pid"
    with log_path.open("ab", 0) as log_handle:
        process = subprocess.Popen(
            command, stdout=log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    util.atomic_write_json(pid_file, {
        "pid": process.pid, "pid_start": util.pid_start_ticks(process.pid),
        "boot_id": util.boot_id(), "launch_id": launch_id,
        "capture_dir": str(capture_dir), "command": command,
    })
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return _fail(f"capture 代理启动后退出: {log_path}")
        health = _proxy_health(cfg, bind_host)
        if health is not None:
            break
        time.sleep(1)
    if health is None:
        return _fail(f"capture 代理未就绪: {log_path}")
    problems = _verify_capture_policy(cfg, health)
    if problems:
        util.terminate_process_tree(process.pid)
        return _fail("capture 代理策略不匹配: " + "; ".join(problems))
    _write_services(cfg, launch_id, bind_host, gateway, manager)
    print(json.dumps({"proxy": "started", "launch_id": launch_id,
                      "pid": process.pid, "health": health}, ensure_ascii=False))
    return 0


def _write_services(cfg: ShellConfig, launch_id: str, bind_host: str, gateway: str,
                    manager) -> None:
    names_service = None
    from shelllib.network import NetworkNames

    names_service = NetworkNames(cfg.scope)
    services = {
        "scope": cfg.scope,
        "launch_id": launch_id,
        "internal_network": names_service.internal,
        "install_network": names_service.install,
        "services_network": names_service.services,
        "services_gateway": gateway,
        "squid_url": f"http://{manager.squid_internal_ip()}:3128",
        "capture_base_url": f"http://{gateway}:{cfg.capture_proxy_port}{cfg.capture_route_prefix}",
        "capture_dir": str(cfg.run_scope / "capture_logs" / launch_id),
        "capture_bind_host": bind_host,
        "capture_port": cfg.capture_proxy_port,
    }
    util.atomic_write_json(cfg.state_dir / "services.json", services)


def cmd_proxy_stop(_args) -> int:
    """停止本 scope capture 代理；核对 PID/start/命令行与所有权。"""
    cfg = load_config_from_env()
    pid_file = cfg.state_dir / "capture_proxy.pid"
    if not pid_file.exists():
        print(json.dumps({"proxy": "not_running"}))
        return 0
    payload = util.read_json(pid_file)
    pid = payload.get("pid")
    if not isinstance(pid, int):
        return _fail("capture PID 文件损坏")
    if payload.get("boot_id") and payload.get("boot_id") != util.boot_id():
        pid_file.unlink(missing_ok=True)
        print(json.dumps({"proxy": "stale_pid_file_cleared"}))
        return 0
    if util.pid_start_ticks(pid) != payload.get("pid_start"):
        pid_file.unlink(missing_ok=True)
        print(json.dumps({"proxy": "stale_pid_file_cleared"}))
        return 0
    command = util.pid_command(pid)
    if str(cfg.capture_proxy_entrypoint) not in command:
        return _fail(f"拒绝停止：PID {pid} 命令行与 capture 代理不匹配: {command[:200]}")
    util.terminate_process_tree(pid)
    pid_file.unlink(missing_ok=True)
    print(json.dumps({"proxy": "stopped", "pid": pid}))
    return 0


def cmd_smoke(_args) -> int:
    """04：最小连通性请求（auxiliary，不混入 benchmark）。"""
    cfg = load_config_from_env()
    services = _read_services(cfg)
    import urllib.error
    import urllib.request

    api_key = os.environ.get(cfg.model_api_key_env, "")
    if not api_key:
        return _fail(f"请在环境中设置 {cfg.model_api_key_env}")

    proxy = urllib.request.ProxyHandler({"http": services["squid_url"], "https": services["squid_url"]})
    opener = urllib.request.build_opener(proxy)
    body = json.dumps({
        "model": cfg.model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "请只回复：连接成功"}],
    }).encode("utf-8")
    request = urllib.request.Request(
        services["capture_base_url"] + "/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-cg-e2e-auxiliary": "smoke-test",
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=cfg.capture_smoke_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _fail(f"连通性检查返回 HTTP {exc.code}: {exc.read()[:500]!r}")
    if not payload.get("content"):
        return _fail(f"响应缺少 content: {str(payload)[:500]}")

    # reasoning effort 实际字段核对（capture 请求体证据，不凭字符串设置断言）
    from shelllib.cc_runner import check_effort_evidence

    evidence = check_effort_evidence(
        Path(services["capture_dir"]) / "raw", cfg.model
    )
    result = {
        "smoke": "passed",
        "model": cfg.model,
        "response_excerpt": str(payload.get("content"))[:200],
        "effort_evidence": evidence,
        "effort_configured": cfg.reasoning_effort,
    }
    if evidence["supported"] is False:
        result["warning"] = (
            "capture 请求体中未发现 reasoning/thinking 字段；上游可能不支持该推理级别，"
            "请检查证据后决定是否继续"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_reverse_test(_args) -> int:
    """反向测试：internal 网络容器允许的路径可用、绕行被阻断。"""
    cfg = load_config_from_env()
    services = _read_services(cfg)
    docker = _docker_control(cfg)
    from shelllib.docker_control import ContainerSpec

    name = f"cg-e2e-nettest-{cfg.scope}"[:63]
    spec = ContainerSpec(
        image="ubuntu:22.04", name=name, role="network-test", scope=cfg.scope,
        network=services["internal_network"],
        command=["sleep", "infinity"],
    )
    try:
        docker.create_and_start(spec)
        results = {}
        # 允许：经 Squid 到 capture 的 HTTP 请求（期望非拒绝）
        squid_url = services["squid_url"]
        capture_base = services["capture_base_url"]
        code, out, err = docker.exec_run(
            name,
            "bash -c 'command -v curl >/dev/null || (apt-get update -qq && apt-get install -y -qq curl >/dev/null 2>&1); "
            f"curl -s -o /dev/null -w %{{http_code}} --max-time 10 -x {squid_url} "
            f"{capture_base}/healthz'",
            timeout=600,
        )
        results["via_squid_to_capture"] = {"exit": code, "output": out.strip(), "stderr": err.strip()[-200:]}
        # 阻断：直连 capture 地址（internal 网络无路由或被 INPUT 规则拒绝）
        code, out, err = docker.exec_run(
            name,
            "bash -c 'curl -s -o /dev/null -w %{{http_code}} --max-time 10 "
            f"--noproxy \"*\" {capture_base}/healthz; echo rc=$?'",
            timeout=120,
        )
        results["direct_to_capture"] = {"exit": code, "output": out.strip(), "stderr": err.strip()[-200:]}
        # 阻断：访问公网（internal 网络 internal=true，无外联路由）
        code, out, err = docker.exec_run(
            name,
            "bash -c 'curl -s -o /dev/null -w %{http_code} --max-time 8 http://example.com/ ; echo rc=$?'",
            timeout=60,
        )
        results["direct_internet"] = {"exit": code, "output": out.strip()}
        verdict = {
            "squid_path_ok": "000" not in (results["via_squid_to_capture"]["output"] or "") and results["via_squid_to_capture"]["exit"] == 0,
            "direct_blocked": "rc=0" not in (results["direct_to_capture"]["output"] or "") or "000" in (results["direct_to_capture"]["output"] or ""),
            "internet_blocked": "rc=0" not in (results["direct_internet"]["output"] or "") or "000" in (results["direct_internet"]["output"] or ""),
        }
        util.atomic_write_json(cfg.state_dir / "reverse_test.json", {"results": results, "verdict": verdict})
        print(json.dumps({"results": results, "verdict": verdict}, ensure_ascii=False, indent=2))
        if not (verdict["squid_path_ok"] and verdict["direct_blocked"] and verdict["internet_blocked"]):
            return _fail("反向测试未全部通过；参见 state/reverse_test.json")
        return 0
    finally:
        with contextlib.suppress(Exception):
            docker.remove_container(name, verify_labels={"cybergym_e2e.scope": cfg.scope})


# ---------------------------------------------------------------------------
# 后处理入口
# ---------------------------------------------------------------------------


def cmd_finalize(args) -> int:
    cfg = load_config_from_env()
    from shelllib.postprocess import finalize_and_summarize

    payload = finalize_and_summarize(cfg, allow_incomplete=bool(args.snapshot))
    print(json.dumps({
        "summary_dir": payload.get("summary_dir"),
        "totals": payload.get("totals"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_online(args) -> int:
    cfg = load_config_from_env()
    from shelllib.online_export import OnlineExporter

    exporter = OnlineExporter(cfg)
    if args.watch:
        exporter.watch(args.interval)
        return 0
    report = exporter.run_once()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_reclassify_run(args) -> int:
    cfg = load_config_from_env()
    stages = [int(item) for item in str(args.success_stages).replace(",", " ").split()]
    if not stages:
        return _fail("--success-stages 不能为空")
    from shelllib.reclassify import reclassify

    report = reclassify(cfg, stages)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CyberGym-E2E 外壳编排")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("check-new")

    init = sub.add_parser("init")
    init.add_argument("--dry-run", action="store_true")

    resume = sub.add_parser("resume")
    resume.add_argument("--strict", action="store_true")

    launch = sub.add_parser("launch")
    launch.add_argument("--dry-run", action="store_true")

    supervise = sub.add_parser("supervise")
    supervise.add_argument("--launch-id", default="")

    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--wait", action="store_true")
    status.add_argument("--wait-timeout", type=float, default=None)
    status.add_argument("--poll", type=float, default=None)

    stop = sub.add_parser("stop")
    stop.add_argument("--cleanup-images", action="store_true")

    cleanup = sub.add_parser("cleanup-scope")
    cleanup.add_argument("--cleanup-images", action="store_true")

    sub.add_parser("recover")
    sub.add_parser("preflight-report")

    sub.add_parser("network-up")
    sub.add_parser("network-down")
    sub.add_parser("proxy-up")
    sub.add_parser("proxy-stop")
    sub.add_parser("smoke")
    sub.add_parser("reverse-test")

    finalize = sub.add_parser("finalize")
    finalize.add_argument("--snapshot", action="store_true")

    online = sub.add_parser("online")
    online.add_argument("--watch", action="store_true")
    online.add_argument("--interval", type=int, default=None)

    reclassify = sub.add_parser("reclassify")
    reclassify.add_argument("--success-stages", required=True)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    handlers = {
        "check-new": cmd_check_new,
        "init": cmd_init,
        "resume": cmd_resume,
        "launch": cmd_launch,
        "supervise": cmd_supervise,
        "status": cmd_status,
        "stop": cmd_stop,
        "cleanup-scope": cmd_cleanup,
        "recover": cmd_recover,
        "preflight-report": cmd_preflight_report,
        "network-up": cmd_network_up,
        "network-down": cmd_network_down,
        "proxy-up": cmd_proxy_up,
        "proxy-stop": cmd_proxy_stop,
        "smoke": cmd_smoke,
        "reverse-test": cmd_reverse_test,
        "finalize": cmd_finalize,
        "online": cmd_online,
        "reclassify": cmd_reclassify_run,
    }
    try:
        return handlers[args.action](args)
    except ConfigError as exc:
        return _fail(f"配置错误: {exc}")
    except RuntimeError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
