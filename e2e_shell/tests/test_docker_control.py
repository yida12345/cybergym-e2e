"""Docker 控制：超时恢复、启动槽、未知同名容器拒绝、所有权删除。"""

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from shelllib.docker_control import (
    ContainerSpec,
    DockerControl,
    DockerControlError,
    UncertainContainerState,
)
from tests.helpers import FakeDockerRunner


def _spec(name="c1", scope="scope1", attempt="a1", image="img:1", network=None):
    return ContainerSpec(
        image=image, name=name, role="agent", scope=scope,
        attempt_id=attempt, launch_id="launch1",
        memory="12g", cpus=2.0, pids_limit=2048, network=network,
        command=["sleep", "infinity"],
    )


class CreateStartTests(unittest.TestCase):
    def test_normal_create_and_start(self):
        runner = FakeDockerRunner()
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        spec = _spec()
        info = docker.create_and_start(spec)
        self.assertEqual(info["State"]["Status"], "running")
        self.assertEqual(info["Labels"]["cybergym_e2e.scope"], "scope1")
        self.assertEqual(info["Labels"]["cybergym_e2e.attempt_id"], "a1")

    def test_timeout_recovers_same_container(self):
        """Docker 超时但 daemon 已创建对象：按名字恢复同一容器。"""
        runner = FakeDockerRunner()
        runner.pull_image("img:1")
        runner.inject_timeout("docker create", times=1)
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        spec = _spec()
        info = docker.create_and_start(spec)
        self.assertEqual(info["State"]["Status"], "running")
        # 只创建了一个容器（没有第二个随机名容器）
        self.assertEqual(len(runner.containers), 1)

    def test_start_timeout_then_recovered(self):
        runner = FakeDockerRunner()
        runner.inject_timeout("docker start", times=1)
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        info = docker.create_and_start(_spec())
        self.assertEqual(info["State"]["Status"], "running")
        self.assertEqual(len(runner.containers), 1)

    def test_exhausted_retries_raise_uncertain(self):
        runner = FakeDockerRunner()
        runner.inject_timeout("docker create", times=10)
        docker = DockerControl(runner=runner, retry_delay_seconds=0, timeout_retries=1)
        with self.assertRaises(UncertainContainerState):
            docker.create_and_start(_spec())

    def test_existing_name_without_recovery_rejected(self):
        """容器名已存在且不是本次恢复：拒绝（不创建第二个）。"""
        runner = FakeDockerRunner()
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        docker.create_and_start(_spec())
        with self.assertRaises(DockerControlError):
            docker.create_and_start(_spec())

    def test_foreign_labels_on_recovery_rejected(self):
        """恢复时发现同名容器与创建记录不匹配（如被外部替换）：拒绝复用。"""
        runner = FakeDockerRunner()
        runner.inject_timeout("docker create", times=1)
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        # 先放一个同名但 scope 不同的容器（模拟外部对象）
        runner.containers["c1"] = {
            "Id": "cid9999", "Name": "c1", "Image": "img:1",
            "Labels": {"cybergym_e2e.scope": "other-scope"},
            "State": {"Status": "created", "ExitCode": 0},
            "Config": {"Image": "img:1", "Labels": {"cybergym_e2e.scope": "other-scope"}},
            "HostConfig": {"Binds": []},
            "NetworkSettings": {"Networks": {}},
        }
        with self.assertRaises(DockerControlError):
            docker.create_and_start(_spec())

    def test_all_stages_use_startup_slot(self):
        """Agent 和所有验证阶段都走同一启动槽入口。"""
        runner = FakeDockerRunner()
        lock_dir = Path(tempfile.mkdtemp(prefix="slots-"))
        # 手工占用全部槽位，验证 create 在拿不到槽时等待
        import fcntl

        holders = []
        for index in range(2):
            handle = (lock_dir / f"slot-{index}.lock").open("a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            holders.append(handle)
        docker = DockerControl(
            runner=runner, startup_lock_dir=lock_dir, startup_concurrency=2,
            retry_delay_seconds=0,
        )
        result = {}

        def try_create():
            try:
                result["info"] = docker.create_and_start(_spec())
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        thread = threading.Thread(target=try_create)
        thread.start()
        time.sleep(0.4)
        # 槽位被占时 create 尚未完成（无 error 也无 info：在启动槽内等待）
        self.assertEqual(result, {})
        # 释放槽位
        for handle in holders:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        thread.join(timeout=10)
        self.assertIn("info", result)


class RemovalTests(unittest.TestCase):
    def test_remove_with_matching_labels(self):
        runner = FakeDockerRunner()
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        docker.create_and_start(_spec())
        self.assertEqual(len(runner.containers), 1)
        self.assertTrue(docker.remove_container("c1", verify_labels={"cybergym_e2e.scope": "scope1"}))
        self.assertEqual(len(runner.containers), 0)

    def test_remove_with_wrong_labels_rejected(self):
        runner = FakeDockerRunner()
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        docker.create_and_start(_spec())
        with self.assertRaises(DockerControlError):
            docker.remove_container("c1", verify_labels={"cybergym_e2e.scope": "not-mine"})


class PullTests(unittest.TestCase):
    def test_pull_missing_image_raises(self):
        runner = FakeDockerRunner()
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        with self.assertRaises(DockerControlError):
            docker.pull("missing:1")

    def test_pull_existing_returns_id(self):
        runner = FakeDockerRunner()
        runner.pull_image("img:1", "sha256:abc")
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        self.assertEqual(docker.pull("img:1"), "sha256:abc")


class ExecTests(unittest.TestCase):
    def test_exec_run_env_and_workdir(self):
        runner = FakeDockerRunner()
        docker = DockerControl(runner=runner, retry_delay_seconds=0)
        docker.create_and_start(_spec())
        code, out, err = docker.exec_run("c1", "echo hi", timeout=10, env={"A": "1"}, workdir="/src")
        self.assertEqual(code, 0)
        command = next(cmd for cmd in runner.commands if cmd[:2] == ["docker", "exec"])
        self.assertIn("-e", command)
        self.assertIn("A=1", command)
        self.assertIn("-w", command)
        self.assertIn("/src", command)
        self.assertIn("timeout", command)  # 容器内 timeout 限额保留


if __name__ == "__main__":
    unittest.main()
