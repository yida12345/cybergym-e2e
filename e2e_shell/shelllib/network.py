"""E2E 独立网络、Squid 代理与宿主 INPUT 规则。

资源命名按 scope hash 派生，全部带 cybergym_e2e.* 所有权 labels：
- 网络：cg-e2e-internal-<scope>（Agent 运行网络，internal）、
  cg-e2e-install-<scope>（可信 prepare 用真实网络）、
  cg-e2e-services-<scope>（capture 侧）。
- 容器：cg-e2e-proxy-<scope>（运行 Squid，只允许 capture 地址）。
- iptables 规则带 scope comment，只允许本 scope 精确删除。

不借用 ExploitGym 的 cybergym-internal-exp / 30304 / 18667；Squid 的 3128
只是容器内部端口，不映射到宿主。capture 默认端口 30305，启动时重新
检查占用。停止只删除本 scope 的规则、网络、服务和容器。
"""

from __future__ import annotations

import contextlib
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .docker_control import ContainerSpec, DockerControl, DockerControlError

IPTABLES = "iptables"

@dataclass
class NetworkNames:
    scope: str

    @property
    def internal(self) -> str:
        return f"cg-e2e-internal-{self.scope}"

    @property
    def install(self) -> str:
        return f"cg-e2e-install-{self.scope}"

    @property
    def services(self) -> str:
        return f"cg-e2e-services-{self.scope}"

    @property
    def squid(self) -> str:
        return f"cg-e2e-proxy-{self.scope}"

    @property
    def rule_comment(self) -> str:
        return f"cg-e2e-{self.scope}"

def port_in_use(host: str, port: int) -> bool:
    """检查端口是否已被占用（capture 启动前重新核对）。"""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(2)
        return probe.connect_ex((host if host else "127.0.0.1", port)) == 0

def _make_iptables_runner(runner: Callable[[list[str], float], object]) -> Callable:
    def run(args: list[str]) -> object:
        return runner([IPTABLES] + args, 30)

    return run

def squid_allow_config(
    *, capture_host: str, capture_port: int, extra_ports: list[int] | None = None
) -> str:
    """运行 Squid 配置：只允许 capture 地址与端口。

    Agent → Squid → capture → 上游 /glm53；其他目标一律拒绝。
    超时覆盖 CC 完整缓存等待（与 proxy/CC 的 timeout 对齐由 config 保证）。
    """
    lines = [
        "# --- cg-e2e run proxy（scope 专属，仅允许 capture 地址） ---",
        "acl SSL_ports port 443",
        f"acl capture_dst dst {capture_host}",
        f"acl capture_port port {capture_port}",
        "acl CONNECT method CONNECT",
        "http_access deny !capture_port",
        "http_access allow capture_dst",
        "http_access allow CONNECT capture_dst",
        "http_access deny all",
        "http_port 3128",
        "cache deny all",
        "access_log stdio:/var/log/squid/access.log",
        "cache_log stdio:/var/log/squid/cache.log",
        # CC 一次完整缓存请求可能等待极长时间；Squid 超时需覆盖该等待。
        "request_timeout 26 hours",
        "read_timeout 26 hours",
        "connect_timeout 60 seconds",
        "client_idle_pconn_timeout 26 hours",
    ]
    return "\n".join(lines) + "\n"

class NetworkManager:
    """scope 网络与 Squid 生命周期；部分失败回滚本次已创建资源。"""

    def __init__(self, docker: DockerControl, names: NetworkNames, *, iptables_runner=None):
        self.docker = docker
        self.names = names
        self._iptables = iptables_runner or _make_iptables_runner(docker.runner)

    # ---- 网络 ----------------------------------------------------------

    def _network_exists(self, name: str) -> bool:
        result = self.docker.runner(["docker", "network", "inspect", name], 60)
        return result.returncode == 0

    def _create_network(self, name: str, *, internal: bool) -> None:
        if self._network_exists(name):
            existing = self.docker.inspect(name) or {}
            labels = existing.get("Labels") or {}
            if labels.get("cybergym_e2e.scope") != self.names.scope:
                raise DockerControlError(
                    f"网络 {name} 已存在但不属于本 scope，拒绝复用"
                )
            return
        command = [
            "docker", "network", "create", "--driver", "bridge",
            "--label", "cybergym_e2e.managed=1",
            "--label", f"cybergym_e2e.scope={self.names.scope}",
        ]
        if internal:
            command.append("--internal")
        command.append(name)
        result = self.docker.runner(command, 120)
        if result.returncode != 0:
            raise DockerControlError(
                f"创建网络失败: {name}: {(result.stderr or '').strip()[-300:]}"
            )

    def network_gateway(self, name: str) -> str:
        result = self.docker.runner(
            ["docker", "network", "inspect", name, "--format",
             "{{(index .IPAM.Config 0).Gateway}}"],
            60,
        )
        if result.returncode != 0:
            raise DockerControlError(f"读取网络网关失败: {name}")
        gateway = (result.stdout or "").strip()
        if not gateway:
            raise DockerControlError(f"网络 {name} 没有网关")
        return gateway

    def network_bridge(self, name: str) -> str:
        """网络对应宿主网桥名；iptables 规则按接口精确删除。"""
        info = self.docker.inspect(name) or {}
        options = info.get("Options") or {}
        bridge = options.get("com.docker.network.bridge.name")
        if bridge:
            return bridge
        network_id = (info.get("Id") or "")[:12]
        if not network_id:
            raise DockerControlError(f"无法确定网络 {name} 的网桥")
        return f"br-{network_id}"

    def up(self, *, capture_host: str, capture_port: int) -> dict[str, str]:
        """创建全部 scope 网络与 Squid；返回服务信息。

        部分失败时回滚本次已创建的资源（roll back created networks and squid）。
        """
        created: list[str] = []
        try:
            self._create_network(self.names.services, internal=False)
            created.append(self.names.services)
            self._create_network(self.names.install, internal=False)
            created.append(self.names.install)
            self._create_network(self.names.internal, internal=True)
            created.append(self.names.internal)
            self._start_squid(capture_host=capture_host, capture_port=capture_port)
        except BaseException:
            self.down()
            raise
        return {
            "internal": self.names.internal,
            "install": self.names.install,
            "services": self.names.services,
            "squid": self.names.squid,
            "services_gateway": self.network_gateway(self.names.services),
        }

    def _start_squid(self, *, capture_host: str, capture_port: int) -> None:
        if self.docker.inspect(self.names.squid) is not None:
            info = self.docker.inspect(self.names.squid) or {}
            labels = (info.get("Config") or {}).get("Labels") or {}
            if labels.get("cybergym_e2e.scope") != self.names.scope:
                raise DockerControlError(
                    f"容器 {self.names.squid} 已存在但不属于本 scope"
                )
            return
        spec = ContainerSpec(
            image="ubuntu/squid:latest",
            name=self.names.squid,
            role="squid",
            scope=self.names.scope,
            network=self.names.internal,
            command=["sh", "-c", "squid -N -f /etc/squid/squid.conf 2>&1"],
        )
        self.docker.create_and_start(spec)
        config = squid_allow_config(capture_host=capture_host, capture_port=capture_port)
        config_path = Path("/tmp") / f"cg-e2e-squid-{self.names.scope}.conf"
        config_path.write_text(config, encoding="utf-8")
        self.docker.copy_to(self.names.squid, config_path, "/etc/squid/squid.conf")
        # 接入 services 网络以便访问 capture 绑定的服务网关地址
        self.docker.switch_network(self.names.squid, None, self.names.services)
        with contextlib.suppress(OSError):
            config_path.unlink()

    def squid_internal_ip(self) -> str:
        info = self.docker.inspect(self.names.squid) or {}
        networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
        internal = networks.get(self.names.internal) or {}
        ip = internal.get("IPAddress")
        if not ip:
            raise DockerControlError(f"Squid 没有 {self.names.internal} 上的地址")
        return ip

    # ---- iptables ------------------------------------------------------

    def block_internal_bridge_to_host(self) -> str:
        """阻断 internal 网桥到宿主 INPUT 的绕行；返回网桥名。

        只添加带 scope comment 的规则；幂等。
        """
        bridge = self.network_bridge(self.names.internal)
        check = self._iptables(
            ["-C", "INPUT", "-i", bridge, "-m", "comment",
             "--comment", self.names.rule_comment, "-j", "REJECT"]
        )
        if check.returncode == 0:
            return bridge
        result = self._iptables(
            ["-I", "INPUT", "-i", bridge, "-m", "comment",
             "--comment", self.names.rule_comment, "-j", "REJECT"]
        )
        if result.returncode != 0:
            raise DockerControlError(
                f"添加 INPUT 规则失败: {bridge}: {(getattr(result, 'stderr', '') or '')}"
            )
        return bridge

    def remove_host_rules(self) -> int:
        """删除本 scope comment 的全部规则；返回删除数量。

        用 iptables -S 拿到规范规则串，逐条替换 -A 为 -D 后精确删除；
        只影响带本 scope comment 的规则。
        """
        removed = 0
        spec = self._iptables(["-S", "INPUT"])
        rules = [
            line.strip()
            for line in ((spec.stdout or "").splitlines())
            if self.names.rule_comment in line
        ]
        for rule in rules:
            delete = rule.replace("-A INPUT", "-D INPUT", 1).split()
            result = self._iptables(delete)
            if result.returncode == 0:
                removed += 1
        return removed

    # ---- 停止 ----------------------------------------------------------

    def down(self) -> dict[str, Any]:
        """回收本 scope 的 Squid、网络与 iptables 规则；报告残留。"""
        report: dict[str, Any] = {"removed_rules": 0, "errors": []}
        try:
            report["removed_rules"] = self.remove_host_rules()
        except Exception as exc:  # noqa: BLE001 - 清理失败要报告而非中断
            report["errors"].append(f"iptables: {exc}")
        try:
            self.docker.remove_container(
                self.names.squid,
                verify_labels={"cybergym_e2e.scope": self.names.scope},
            )
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"squid: {exc}")
        for name in (self.names.internal, self.names.install, self.names.services):
            if self._network_exists(name):
                result = self.docker.runner(["docker", "network", "rm", name], 120)
                if result.returncode != 0:
                    report["errors"].append(
                        f"network {name}: {(result.stderr or '').strip()[-200:]}"
                    )
        return report
