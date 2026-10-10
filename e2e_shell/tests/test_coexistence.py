"""共存：E2E 启动/停止只作用于自身资源，不影响 ExploitGym 的容器/网络/端口。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from shelllib.docker_control import DockerControl
from shelllib.network import NetworkManager, NetworkNames, port_in_use
from tests.helpers import FakeDockerRunner, make_config, make_fake_dataset

# 只读基线：现有 ExploitGym 资源名（设计文档 §12）。
EXPLOITGYM_RESOURCES = [
    "cybergym-internal-exp",
    "cybergym-install-exp",
    "cybergym-services-exp",
    "cybergym-proxy-exp",
    "cybergym-install-proxy-exp",
]


def _harness(tmp: Path):
    make_fake_dataset(tmp)
    from shelllib import manifest as manifest_mod

    cfg = make_config(tmp)
    cfg.derive()
    manifest_mod.prepare_new_experiment(cfg)
    runner = FakeDockerRunner()
    docker = DockerControl(runner=runner, retry_delay_seconds=0,
                           startup_lock_dir=cfg.docker_startup_lock_dir,
                           startup_concurrency=4)
    return cfg, runner, docker


class CoexistenceTests(unittest.TestCase):
    def test_scope_containers_removed_not_others(self):
        """停止只删除本 scope labels 的容器；其他容器原样保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner, docker = _harness(Path(tmp))
            # E2E 容器
            from shelllib.docker_control import ContainerSpec

            docker.create_and_start(ContainerSpec(
                image="img:1", name="cg-e2e-agent-mine", role="agent", scope=cfg.scope))
            # 其他 scope 的容器（模拟 ExploitGym / 其他实验）
            runner.containers["exp-agent-foreign"] = {
                "Id": "cid-foreign", "Name": "exp-agent-foreign", "Image": "img:1",
                "Labels": {"cybergym.run_id": "other-run"},
                "State": {"Status": "running", "ExitCode": 0},
                "Config": {"Image": "img:1", "Labels": {"cybergym.run_id": "other-run"}},
                "HostConfig": {"Binds": []},
                "NetworkSettings": {"Networks": {}},
            }
            # 通过 stop 路径清理本 scope
            removed = 0
            for container in docker.list_scope_containers(cfg.scope):
                docker.remove_container(container, verify_labels={"cybergym_e2e.scope": cfg.scope})
                removed += 1
            self.assertEqual(removed, 1)
            self.assertIn("exp-agent-foreign", runner.containers)  # 其他容器保留

    def test_network_down_removes_only_scoped(self):
        """network down 只删除本 scope 的网络；ExploitGym 网络不受影响。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner, docker = _harness(Path(tmp))
            # 预置 ExploitGym 网络与容器（只读基线）
            for name in EXPLOITGYM_RESOURCES:
                runner.networks[name] = {
                    "Id": f"expnet-{len(runner.networks)}", "Name": name,
                    "Labels": {}, "Options": {}, "IPAM": {"Config": [{"Gateway": "172.18.0.1"}]},
                }
            # E2E scope 网络
            manager = NetworkManager(docker, NetworkNames(cfg.scope))
            manager.up(capture_host="127.0.0.1", capture_port=30305)
            self.assertIn(cfg.scope, json.dumps(list(runner.networks)))
            before = set(runner.networks)
            report = manager.down()
            self.assertEqual(report["errors"], [])
            after = set(runner.networks)
            removed = before - after
            # 只移除了本 scope 的三个网络
            scope_networks = {
                NetworkNames(cfg.scope).internal,
                NetworkNames(cfg.scope).install,
                NetworkNames(cfg.scope).services,
            }
            self.assertEqual(removed, scope_networks)
            for name in EXPLOITGYM_RESOURCES:
                self.assertIn(name, after)  # ExploitGym 网络保留

    def test_capture_port_not_reused(self):
        """E2E capture 端口默认 30305，与 ExploitGym 的 30304/18667 不同。"""
        self.assertEqual(make_config(Path("/tmp/unused-coexistence")).capture_proxy_port, 30305)
        self.assertNotEqual(30305, 30304)
        self.assertNotEqual(30305, 18667)

    def test_port_in_use_detection(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            # 自己占用后 connect_ex 探测应成功（认为被占用）
            self.assertTrue(port_in_use("127.0.0.1", port))
        self.assertFalse(port_in_use("127.0.0.1", 59999))

    def test_no_global_kill_or_prune_in_stop(self):
        """08 停止路径不使用 pkill/全局 prune；命令全部带 scope 过滤。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner, docker = _harness(Path(tmp))
            for _index in range(3):
                from shelllib.docker_control import ContainerSpec

                docker.create_and_start(ContainerSpec(
                    image="img:1", name=f"cg-e2e-agent-{_index}", role="agent", scope=cfg.scope))
            before = len(runner.containers)
            for container in docker.list_scope_containers(cfg.scope):
                docker.remove_container(container, verify_labels={"cybergym_e2e.scope": cfg.scope})
            self.assertEqual(before, 3)
            self.assertEqual(len(runner.containers), 0)
            forbidden = ("prune", "pkill", "killall")
            for command in runner.commands:
                joined = " ".join(command)
                for keyword in forbidden:
                    self.assertNotIn(keyword, joined, f"命令使用了危险操作: {joined}")

    def test_remove_foreign_container_rejected(self):
        """labels 不匹配的容器删除被拒绝（所有权保护）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner, docker = _harness(Path(tmp))
            runner.containers["foreign"] = {
                "Id": "cidf", "Name": "foreign", "Image": "img:1",
                "Labels": {"cybergym_e2e.scope": "someone-else"},
                "State": {"Status": "running"},
                "Config": {"Image": "img:1",
                           "Labels": {"cybergym_e2e.scope": "someone-else"}},
                "HostConfig": {"Binds": []}, "NetworkSettings": {"Networks": {}},
            }
            with self.assertRaises(Exception):
                docker.remove_container("foreign", verify_labels={"cybergym_e2e.scope": cfg.scope})
            self.assertIn("foreign", runner.containers)


if __name__ == "__main__":
    unittest.main()
