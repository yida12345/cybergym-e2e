import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror_images as mirror


CONFIG = "sha256:" + "a" * 64
SOURCE = "sha256:" + "b" * 64
TARGET = "sha256:" + "c" * 64
OTHER = "sha256:" + "d" * 64


def verbose_manifest(manifest=SOURCE, config=CONFIG, arch="amd64", variant=None):
    platform = {"os": "linux", "architecture": arch}
    if variant:
        platform["variant"] = variant
    return {"Descriptor": {"digest": manifest, "platform": platform},
            "SchemaV2Manifest": {"config": {"digest": config}}}


def arguments(root, **overrides):
    values = dict(output_dir=root / "output", lock_dir=root / "locks", platform="linux/amd64",
                  keep_local_images=False, workers=3, push_workers=1, docker=sys.executable,
                  retries=2, retry_delay=0, command_timeout=5, probe_timeout=5,
                  copy_backend="auto", skopeo=sys.executable, skopeo_authfile=None, skopeo_policy=None)
    return argparse.Namespace(**{**values, **overrides})


def image(source="upstream/image:one"):
    target = mirror.target_ref(source, "acr.example/ns/e2e", "linux/amd64", "")
    key = hashlib.sha256((source + target).encode()).hexdigest()
    return mirror.Image(key, source, target, ["demo/task/config.toml"])


class FakeDocker:
    """Model a registry and local refs; operations expose real pipeline effects."""

    registry_copy = mirror.Docker.registry_copy

    def __init__(self, args, stop):
        self.args = args
        self.stop = stop
        self.local = {}
        self.remote = {}
        self.calls = []
        self.manifest_calls = []
        self.fail_push = False
        self.missing_push_content = False
        self.fail_copy = False
        self.bad_copy_digest = False
        self.copy_policy = None
        self.fail_pull = False
        self.cancel_push = False
        self.fail_cleanup = False
        self.bad_push_digest = False
        self.manifest_ids = False
        self.delay = 0
        self.active_pushes = 0
        self.max_pushes = 0
        self.active_pulls = 0
        self.max_pulls = 0
        self.lock = threading.Lock()

    def manifest(self, ref, platform, log, *, optional=False):
        self.manifest_calls.append(ref)
        if optional:
            return self.remote.get(ref)
        if ref in self.remote:
            return self.remote[ref]
        return mirror.Manifest(SOURCE, CONFIG, platform)

    def image_id(self, ref, log, *, ignore_stop=False):
        return self.local.get(ref)

    def command(self, args, log, **kwargs):
        with self.lock:
            self.calls.append(args)
        if kwargs.get("executable"):
            self.copy_policy = json.loads(Path(args[args.index("--policy") + 1]).read_text())
            if self.fail_copy:
                raise mirror.MirrorError("registry copy failed")
            self.remote[args[-1].removeprefix("docker://")] = mirror.Manifest(SOURCE, CONFIG, "linux/amd64")
            Path(args[args.index("--digestfile") + 1]).write_text(OTHER if self.bad_copy_digest else SOURCE)
            return "Copying config; Writing manifest to image destination\n"
        if args[0] == "info":
            return "29.7.2\n"
        if args == ["push", "--help"]:
            return "Usage: docker push [--platform string] NAME[:TAG]\n"
        if args[0] == "pull":
            with self.lock:
                self.active_pulls += 1
                self.max_pulls = max(self.max_pulls, self.active_pulls)
            time.sleep(self.delay)
            self.local[args[-1]] = SOURCE if self.manifest_ids else CONFIG
            with self.lock:
                self.active_pulls -= 1
            if self.fail_pull:
                raise mirror.MirrorError("pull failed after creating local ref")
        elif args[0] == "tag":
            if args[1] not in self.local:
                raise mirror.MirrorError("source ref was removed by another job")
            self.local[args[2]] = self.local[args[1]]
        elif args[0] == "push":
            with self.lock:
                self.active_pushes += 1
                self.max_pushes = max(self.max_pushes, self.active_pushes)
            try:
                time.sleep(self.delay)
                if self.cancel_push:
                    self.stop.set()
                    raise mirror.Cancelled("push interrupted")
                if self.fail_push:
                    raise mirror.MirrorError("registry unavailable")
                if self.missing_push_content:
                    raise mirror.PushContentUnavailable("was found but does not provide the specified platform (linux/amd64)")
                self.remote[args[-1]] = mirror.Manifest(TARGET, CONFIG, "linux/amd64")
                return f"tag: digest: {OTHER if self.bad_push_digest else TARGET} size: 123\n"
            finally:
                with self.lock:
                    self.active_pushes -= 1
        elif args[:2] == ["image", "rm"]:
            if self.fail_cleanup:
                raise mirror.MirrorError("image is in use")
            del self.local[args[2]]
        return ""


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = arguments(self.root)
        self.stop = threading.Event()
        self.docker = FakeDocker(self.args, self.stop)

    def mirror(self, item=None):
        with patch("mirror_images.emit"):
            return mirror.mirror_image(item or image(), self.args, self.docker,
                                       threading.Semaphore(1), self.stop)

    def test_collection_honors_override_fallback_and_project_images(self):
        repo = self.root / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts/pull_images.py").write_text('DEFAULT_BUILD_IMAGE = "base@sha256:abc"\n')
        for task in ("one/a", "one/b", "two/c"):
            path = repo / "projects" / task
            path.mkdir(parents=True)
            (path / "config.toml").write_text('build_image = "override:tag"\n' if task == "one/b" else "")
        (repo / "projects/one/project.toml").write_text('build_image = "project:tag"\n')
        (repo / "projects/two/project.toml").write_text('repo_to_patch = "two"\n')
        refs, tasks = mirror.collect_images(repo)
        self.assertEqual(set(refs), {"project:tag", "override:tag", "base@sha256:abc"})
        self.assertEqual(tasks["one/b"], "override:tag")
        refs, tasks = mirror.collect_images(repo, {"one/b"})
        self.assertEqual(set(refs), {"project:tag", "override:tag"})
        self.assertEqual(set(tasks), {"one/b"})
        with self.assertRaises(mirror.MirrorError):
            mirror.collect_images(repo, {"one/missing"})

    def test_task_file_rejects_traversal_and_ambiguous_paths(self):
        path = self.root / "tasks.txt"
        for content in ("../task", "/one/task", "one/./task", "one//task", ""):
            path.write_text(content)
            with self.assertRaises(mirror.MirrorError, msg=content):
                mirror.read_tasks(path)

    def test_tags_distinguish_digest_registry_platform_and_long_names(self):
        refs = ["one/img:tag", "two/img:tag", "registry:5000/img:tag", "one/img@" + SOURCE]
        names = {mirror.target_ref(ref, "acr/ns/repo", platform, "v1-")
                 for ref in refs for platform in ("linux/amd64", "linux/arm64")}
        self.assertEqual(len(names), 8)
        long_name = mirror.target_ref("x/" + "a" * 300 + ":v", "acr/ns/repo", "linux/amd64", "v1-")
        self.assertLessEqual(len(long_name.rsplit(":", 1)[1]), 128)
        self.assertEqual(mirror.repository_of("registry:5000/img:tag"), "registry:5000/img")

    def test_manifest_selects_requested_platform_and_rejects_missing_config(self):
        payload = [verbose_manifest(arch="arm64", variant="v8"), verbose_manifest()]
        self.assertEqual(mirror.parse_manifest(json.dumps(payload), "linux/amd64").digest, SOURCE)
        self.assertEqual(mirror.parse_manifest(json.dumps(payload), "linux/arm64/v8").platform, "linux/arm64/v8")
        with self.assertRaises(mirror.MirrorError):
            mirror.parse_manifest(json.dumps(payload), "linux/arm64/v7")
        with self.assertRaises(mirror.MirrorError):
            mirror.parse_manifest(json.dumps({"Descriptor": payload[1]["Descriptor"]}), "linux/amd64")

    def test_success_verifies_and_cleans_only_owned_refs(self):
        self.docker.local["unrelated:tag"] = OTHER
        result = self.mirror()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["target_manifest_digest"], TARGET)
        self.assertEqual(self.docker.local, {"unrelated:tag": OTHER})
        self.assertTrue(any(call[:3] == ["pull", "--platform", "linux/amd64"] for call in self.docker.calls))
        self.assertTrue(any(call[:3] == ["push", "--platform", "linux/amd64"] for call in self.docker.calls))

    def test_existing_task_source_is_cleaned(self):
        ref = "upstream/image@" + SOURCE
        self.docker.local[ref] = CONFIG
        self.assertEqual(self.mirror()["status"], "success")
        self.assertEqual(self.docker.local, {})
        self.assertFalse(any(call[0] == "pull" for call in self.docker.calls))

    def test_missing_push_content_falls_back_to_verified_registry_copy(self):
        self.docker.missing_push_content = True
        result = self.mirror()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["copy_backend"], "skopeo")
        self.assertEqual(result["target_manifest_digest"], SOURCE)
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})
        copies = [call for call in self.docker.calls if "copy" in call]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][-2], "docker://upstream/image@" + SOURCE)
        self.assertIn("--preserve-digests", copies[0])
        self.assertFalse(list((self.args.output_dir / "logs").glob("registry-copy-*")))

    def test_direct_copy_skips_local_pull_and_cleans_existing_refs(self):
        self.args.copy_backend = "skopeo"
        item = image()
        self.docker.local.update({item.source: CONFIG, item.target: CONFIG,
                                  "upstream/image@" + SOURCE: CONFIG, "unrelated:tag": OTHER})
        authfile = self.root / "credentials.json"
        self.args.skopeo_authfile = authfile
        result = self.mirror(item)
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.docker.local, {"unrelated:tag": OTHER})
        self.assertFalse(any(call[0] in {"pull", "tag", "push"} for call in self.docker.calls))
        copy = next(call for call in self.docker.calls if "copy" in call)
        self.assertEqual(copy[copy.index("--authfile") + 1], str(authfile))

    def test_failed_registry_copy_cleans_and_remembers_backend_on_resume(self):
        self.docker.missing_push_content = True
        self.docker.fail_copy = True
        result = self.mirror()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})
        self.assertFalse(list((self.args.output_dir / "logs").glob("registry-copy-*")))
        self.docker.calls.clear()
        self.docker.fail_copy = False
        self.assertEqual(self.mirror()["status"], "success")
        self.assertFalse(any(call[0] in {"pull", "tag", "push"} for call in self.docker.calls))

    def test_registry_copy_rejects_changed_digest(self):
        self.args.copy_backend = "skopeo"
        self.docker.bad_copy_digest = True
        result = self.mirror()
        self.assertEqual(result["status"], "failed")
        self.assertIn("did not preserve", result["error"])
        self.assertTrue(result["cleanup_complete"])

    def test_registry_copy_creates_temporary_policy_only_for_pinned_source(self):
        self.args.copy_backend = "skopeo"
        with patch("mirror_images.Path.is_file", return_value=False):
            self.assertEqual(self.mirror()["status"], "success")
        self.assertEqual(self.docker.copy_policy, {
            "default": [{"type": "reject"}], "transports": {"docker": {
                "docker.io/upstream/image@" + SOURCE: [{"type": "insecureAcceptAnything"}],
            }},
        })
        self.assertFalse(list((self.args.output_dir / "logs").glob("registry-copy-*")))

    def test_registry_copy_honors_explicit_policy(self):
        self.args.copy_backend = "skopeo"
        self.args.skopeo_policy = self.root / "custom-policy.json"
        policy = {"default": [{"type": "reject"}]}
        self.args.skopeo_policy.write_text(json.dumps(policy))
        self.assertEqual(self.mirror()["status"], "success")
        self.assertEqual(self.docker.copy_policy, policy)
        self.assertEqual(json.loads(self.args.skopeo_policy.read_text()), policy)

    def test_registry_copy_honors_existing_host_policy(self):
        self.args.copy_backend = "skopeo"
        config_dir = self.root / "config"
        policy_path = config_dir / "containers/policy.json"
        policy_path.parent.mkdir(parents=True)
        policy = {"default": [{"type": "reject"}]}
        policy_path.write_text(json.dumps(policy))
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_dir)}):
            self.assertEqual(self.mirror()["status"], "success")
        self.assertEqual(self.docker.copy_policy, policy)
        self.assertEqual(json.loads(policy_path.read_text()), policy)

    def test_copy_policy_normalizes_source_and_requires_digest(self):
        for ref, normalized in (
            ("ubuntu", "docker.io/library/ubuntu"), ("n132/arvo", "docker.io/n132/arvo"),
            ("gcr.io/oss-fuzz-base/base-builder", "gcr.io/oss-fuzz-base/base-builder"),
        ):
            policy = mirror.pinned_copy_policy(ref + "@" + SOURCE)
            self.assertEqual(list(policy["transports"]["docker"]), [normalized + "@" + SOURCE])
        with self.assertRaises(mirror.MirrorError):
            mirror.pinned_copy_policy("n132/arvo:123")

    def test_docker_only_backend_does_not_fallback(self):
        self.args.copy_backend = "docker"
        self.docker.missing_push_content = True
        result = self.mirror()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any("copy" in call for call in self.docker.calls))
        self.assertTrue(result["cleanup_complete"])

    def test_missing_skopeo_cleans_after_fallback(self):
        self.docker.missing_push_content = True
        with patch("mirror_images.shutil.which", return_value=None):
            result = self.mirror()
        self.assertEqual(result["status"], "failed")
        self.assertIn("requires skopeo", result["error"])
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})

    def test_registry_copy_rate_limit_stops_and_cleans(self):
        self.args.copy_backend = "skopeo"
        self.docker.local["upstream/image@" + SOURCE] = CONFIG
        with patch.object(self.docker, "registry_copy", side_effect=mirror.RateLimited("toomanyrequests")):
            result = self.mirror()
        self.assertEqual(result["status"], "rate_limited")
        self.assertTrue(self.stop.is_set())
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})

    def test_registry_copy_cancellation_still_cleans(self):
        self.args.copy_backend = "skopeo"
        self.docker.local["upstream/image@" + SOURCE] = CONFIG
        with patch.object(self.docker, "registry_copy", side_effect=mirror.Cancelled("interrupted")):
            result = self.mirror()
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})

    def test_push_platform_content_error_does_not_retry(self):
        executable = self.root / "fake-docker"
        executable.write_text("#!/bin/sh\nprintf '%s\\n' 'image was found but does not provide the specified platform (linux/amd64)'\nexit 1\n")
        executable.chmod(0o700)
        self.args.docker = str(executable)
        docker = mirror.Docker(self.args, self.stop)
        log = self.root / "content-error.log"
        with self.assertRaises(mirror.PushContentUnavailable):
            docker.command(["push", "--platform", "linux/amd64", "target:tag"], log)
        self.assertEqual(log.read_text().count("$ "), 1)

    def test_containerd_manifest_image_id_supported(self):
        self.docker.manifest_ids = True
        self.assertEqual(self.mirror()["status"], "success")
        self.assertEqual(self.docker.local, {})

    def test_existing_task_target_is_cleaned(self):
        item = image()
        self.docker.local[item.target] = CONFIG
        self.assertEqual(self.mirror(item)["status"], "success")
        self.assertEqual(self.docker.local, {})

    def test_local_target_conflict_is_not_retagged(self):
        item = image()
        self.docker.local[item.target] = OTHER
        result = self.mirror(item)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.docker.local[item.target], OTHER)
        self.assertFalse(any(call[0] in {"tag", "push"} for call in self.docker.calls))

    def test_resume_checks_remote_without_downloading_layers(self):
        item = image()
        self.docker.remote[item.target] = mirror.Manifest(TARGET, CONFIG, "linux/amd64")
        self.assertEqual(self.mirror(item)["status"], "skipped")
        self.assertEqual(self.docker.calls, [])

    def test_remote_conflict_is_never_overwritten(self):
        item = image()
        self.docker.remote[item.target] = mirror.Manifest(TARGET, OTHER, "linux/amd64")
        result = self.mirror(item)
        self.assertEqual(result["status"], "failed")
        self.assertIn("refusing overwrite", result["error"])
        self.assertEqual(self.docker.calls, [])

    def test_verified_resume_never_queries_source_registry(self):
        item = image()
        self.assertEqual(self.mirror(item)["status"], "success")
        self.docker.calls.clear()
        self.docker.manifest_calls.clear()
        result = self.mirror(item)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["verification_mode"], "acr-state")
        self.assertEqual(self.docker.manifest_calls, [item.target])
        self.assertEqual(self.docker.calls, [])

    def test_verified_resume_rejects_changed_acr_digest(self):
        item = image()
        self.assertEqual(self.mirror(item)["status"], "success")
        self.docker.remote[item.target] = mirror.Manifest(OTHER, CONFIG, "linux/amd64")
        self.docker.manifest_calls.clear()
        self.docker.calls.clear()
        result = self.mirror(item)
        self.assertEqual(result["status"], "failed")
        self.assertIn("differs from verified state", result["error"])
        self.assertEqual(self.docker.manifest_calls, [item.target])
        self.assertEqual(self.docker.calls, [])

    def test_missing_acr_on_resume_repairs_from_source(self):
        item = image()
        self.assertEqual(self.mirror(item)["status"], "success")
        self.docker.remote.clear()
        self.docker.manifest_calls.clear()
        self.docker.calls.clear()
        self.assertEqual(self.mirror(item)["status"], "success")
        self.assertIn(item.source, self.docker.manifest_calls)
        self.assertTrue(any(call[0] == "pull" for call in self.docker.calls))

    def test_refresh_source_explicitly_rechecks_verified_source(self):
        item = image()
        self.assertEqual(self.mirror(item)["status"], "success")
        self.args.refresh_source = True
        self.docker.manifest_calls.clear()
        self.assertEqual(self.mirror(item)["status"], "skipped")
        self.assertIn(item.source, self.docker.manifest_calls)

    def test_rate_limit_during_push_stops_dispatch_and_still_cleans(self):
        original_command = self.docker.command

        def command(args, log, **kwargs):
            if args[0] == "push":
                raise mirror.RateLimited("toomanyrequests: pull rate limit")
            return original_command(args, log, **kwargs)

        with patch.object(self.docker, "command", side_effect=command):
            result = self.mirror()
        self.assertEqual(result["status"], "rate_limited")
        self.assertTrue(result["rate_limited"])
        self.assertTrue(result["cleanup_complete"])
        self.assertTrue(self.stop.is_set())
        self.assertEqual(self.docker.local, {})

    def test_rate_limit_scheduler_saves_progress_and_stops_next_image(self):
        self.args.workers = 1
        jobs = [image(f"upstream/image{index}:tag") for index in range(3)]
        with patch.object(self.docker, "manifest", side_effect=mirror.RateLimited("pull rate limit")), \
                patch("mirror_images.Docker", return_value=self.docker), patch("mirror_images.emit"):
            result = mirror.run_mirror(jobs, self.args, {"task_images": {}})
        self.assertEqual(result, 1)
        report = json.loads((self.args.output_dir / "report.json").read_text())
        self.assertEqual(report["stop_reason"], "rate_limit")
        self.assertTrue(report["rate_limited"])
        self.assertEqual(report["completed_images"], 1)
        self.assertEqual(report["pending_images"], 2)

    def test_rate_limit_command_does_not_retry(self):
        docker = mirror.Docker(self.args, self.stop)
        code = "import sys; print('toomanyrequests: unauthenticated pull rate limit'); sys.exit(1)"
        log = self.root / "quota.log"
        with self.assertRaises(mirror.RateLimited):
            docker.command(["-c", code], log)
        self.assertEqual(log.read_text().count("$ "), 1)
        self.assertTrue(self.stop.is_set())

    def test_failed_push_cleans_refs_and_resume_pulls_again(self):
        self.docker.fail_push = True
        self.assertEqual(self.mirror()["status"], "failed")
        self.assertEqual(self.docker.local, {})
        self.assertFalse(self.stop.is_set())
        self.stop.clear()
        self.docker.fail_push = False
        self.docker.calls.clear()
        self.assertEqual(self.mirror()["status"], "success")
        self.assertEqual(self.docker.local, {})
        self.assertTrue(any(call[0] == "pull" for call in self.docker.calls))

    def test_bad_push_digest_cleans_local_refs(self):
        self.docker.bad_push_digest = True
        result = self.mirror()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Push digest differs", result["error"])
        self.assertEqual(self.docker.local, {})

    def test_failed_pull_cleans_created_ref(self):
        self.docker.fail_pull = True
        result = self.mirror()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})

    def test_cancelled_push_still_cleans_refs(self):
        self.docker.cancel_push = True
        result = self.mirror()
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})

    def test_source_probe_failure_cleans_historical_failed_run(self):
        item = image()
        ref = "upstream/image@" + SOURCE
        self.docker.local.update({ref: CONFIG, item.target: CONFIG})
        mirror.atomic_json(self.args.output_dir / "state" / f"{item.key}.json", {
            "schema_version": 1, "source": item.source, "target": item.target,
            "platform": self.args.platform, "source_manifest_digest": SOURCE,
            "config_digest": CONFIG, "owned_local_refs": {ref: CONFIG, item.target: CONFIG},
        })
        with patch.object(self.docker, "manifest", side_effect=mirror.MirrorError("source unavailable")):
            result = self.mirror(item)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(self.docker.local, {})

    def test_skipped_remote_cleans_existing_task_refs(self):
        item = image()
        self.docker.remote[item.target] = mirror.Manifest(TARGET, CONFIG, "linux/amd64")
        self.docker.local.update({item.source: CONFIG, "upstream/image@" + SOURCE: CONFIG,
                                  item.target: CONFIG, "unrelated:tag": OTHER})
        self.assertEqual(self.mirror(item)["status"], "skipped")
        self.assertEqual(self.docker.local, {"unrelated:tag": OTHER})

    def test_cleanup_failure_stops_new_jobs(self):
        self.docker.fail_cleanup = True
        result = self.mirror()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(self.stop.is_set())
        self.assertEqual(len(result["cleanup_errors"]), 2)

    def test_distinct_tags_sharing_digest_do_not_race_cleanup(self):
        self.docker.delay = 0.02
        jobs = [image(f"upstream/image:{tag}") for tag in ("one", "two")]
        with patch("mirror_images.emit"), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(mirror.mirror_image, job, self.args, self.docker,
                                   threading.Semaphore(2), self.stop) for job in jobs]
            self.assertEqual([f.result()["status"] for f in futures], ["success", "success"])
        self.assertEqual(self.docker.max_pulls, 1)
        self.assertEqual(self.docker.local, {})

    def test_scheduler_bounds_pull_and_push_concurrency_and_writes_mapping(self):
        self.docker.delay = 0.03
        jobs = [image(f"upstream/image{index}:tag") for index in range(6)]
        with patch("mirror_images.Docker", return_value=self.docker), patch("mirror_images.emit"):
            result = mirror.run_mirror(jobs, self.args, {"task_images": {"demo/one": jobs[0].source}})
        self.assertEqual(result, 0)
        self.assertGreater(self.docker.max_pulls, 1)
        self.assertLessEqual(self.docker.max_pulls, 3)
        self.assertEqual(self.docker.max_pushes, 1)
        mapping = json.loads((self.args.output_dir / "image_mapping.json").read_text())
        self.assertTrue(mapping["complete"])
        self.assertEqual(len(mapping["source_to_acr"]), 6)
        report = json.loads((self.args.output_dir / "report.json").read_text())
        self.assertEqual(report["counts"], {"success": 6})

    def test_command_retries_transient_failure(self):
        counter = self.root / "attempts"
        code = ("import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                "n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); "
                "print('temporary gateway failure' if n==1 else 'OK'); sys.exit(n==1)")
        docker = mirror.Docker(self.args, self.stop)
        with patch("mirror_images.emit"):
            output = docker.command(["-c", code, str(counter)], self.root / "retry.log")
        self.assertEqual(output.strip(), "OK")
        self.assertEqual(counter.read_text(), "2")

    def test_authentication_failure_is_not_missing_manifest(self):
        docker = mirror.Docker(self.args, self.stop)
        code = "import sys; print('unauthorized: manifest unknown; helper not found'); sys.exit(1)"
        with self.assertRaises(mirror.MirrorError):
            docker.command(["-c", code], self.root / "auth.log", missing=mirror.REMOTE_MISSING)
        self.assertEqual((self.root / "auth.log").read_text().count("$ "), 1)

    def test_command_cancellation_terminates_subprocess(self):
        docker = mirror.Docker(self.args, self.stop)
        timer = threading.Timer(0.15, self.stop.set)
        timer.start()
        try:
            with self.assertRaises(mirror.Cancelled):
                docker.command(["-c", "import time; time.sleep(10)"], self.root / "cancel.log")
        finally:
            timer.cancel()

    def test_dry_run_never_creates_docker_client(self):
        repo = Path(__file__).resolve().parents[2] / "cybergym-e2e"
        if not repo.is_dir():
            self.skipTest("official checkout unavailable")
        with patch("mirror_images.Docker", side_effect=AssertionError("Docker used")), patch("mirror_images.emit"):
            self.assertEqual(mirror.main(["--repo-dir", str(repo), "--output-dir", str(self.root / "dry"),
                                          "--dry-run"]), 0)
        plan = json.loads((self.root / "dry/plan.json").read_text())
        self.assertEqual(plan["selected_images"], len(plan["images"]))
        self.assertGreater(plan["selected_images"], 0)


if __name__ == "__main__":
    unittest.main()
