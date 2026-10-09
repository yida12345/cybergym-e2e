#!/usr/bin/env python3
"""Mirror official CyberGym-E2E build images to ACR without rebuilding them."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE.parent / "cybergym-e2e"
DEFAULT_REGISTRY = "qdyd-jingxiang-registry-vpc.ap-northeast-1.cr.aliyuncs.com"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REMOTE_MISSING = ("no such manifest", "manifest unknown", "manifest_unknown")
LOCAL_MISSING = ("no such image", "no such object")
FATAL_ERRORS = ("unauthorized", "authentication required", "denied:",
                "invalid reference format", "repository does not exist")
PRINT_LOCK = threading.Lock()


class MirrorError(RuntimeError):
    pass


class Cancelled(MirrorError):
    pass


class RateLimited(MirrorError):
    """A registry quota cannot be recovered by short command retries."""


class PushContentUnavailable(MirrorError):
    """Docker cannot select a complete local manifest for pushing."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise MirrorError(f"{label}: missing or invalid sha256 digest: {value!r}")
    return value


def repository_of(ref: str) -> str:
    base = ref.split("@", 1)[0]
    # A colon in the last path component is a tag, not a registry port.
    last_slash = base.rfind("/")
    last_colon = base.rfind(":")
    return base[:last_colon] if last_colon > last_slash else base


def pinned_copy_policy(source: str) -> dict:
    repository, separator, manifest_digest = source.partition("@")
    if not separator:
        raise MirrorError("Registry copy policy requires a digest-pinned source")
    digest(manifest_digest, "registry copy source")
    first = repository.split("/", 1)[0]
    if "/" not in repository:
        repository = "docker.io/library/" + repository
    elif "." not in first and ":" not in first and first != "localhost":
        repository = "docker.io/" + repository
    return {"default": [{"type": "reject"}], "transports": {
        "docker": {f"{repository}@{manifest_digest}": [{"type": "insecureAcceptAnything"}]},
    }}


def default_build_image(repo: Path) -> str:
    """Read the official fallback without importing its Docker SDK side effects."""
    path = repo / "scripts" / "pull_images.py"
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_BUILD_IMAGE"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value:
                return value
    raise MirrorError(f"DEFAULT_BUILD_IMAGE not found in {path}")


def read_tasks(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    tasks = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        task = line.split("#", 1)[0].strip()
        if not task:
            continue
        parts = PurePosixPath(task).parts
        if len(parts) != 2 or task != "/".join(parts) or any(p in {".", "..", "/"} for p in parts) or "\\" in task:
            raise MirrorError(f"{path}:{line_number}: expected project/task, got {task!r}")
        tasks.add(task)
    if not tasks:
        raise MirrorError(f"Empty task file: {path}")
    return tasks


def collect_images(repo: Path, tasks: set[str] | None = None) -> tuple[dict, dict]:
    """Match official pull_images.py, including project-level build images."""
    projects = repo / "projects"
    project_files = sorted(projects.glob("*/project.toml"))
    if not project_files:
        raise MirrorError(f"No */project.toml files in {projects}")
    fallback = default_build_image(repo)
    images: dict[str, set[str]] = {}
    task_images: dict[str, str] = {}

    def add(ref: str, path: Path) -> None:
        if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c.isspace() for c in ref):
            raise MirrorError(f"Invalid build_image in {path}: {ref!r}")
        images.setdefault(ref, set()).add(str(path.relative_to(projects)))

    for project_file in project_files:
        config = tomllib.loads(project_file.read_text(encoding="utf-8"))
        task_files = sorted(project_file.parent.glob("*/config.toml"))
        selected = [p for p in task_files if tasks is None or str(p.parent.relative_to(projects)) in tasks]
        if tasks is not None and not selected:
            continue
        if config.get("build_image"):
            add(config["build_image"], project_file)
        for task_file in selected:
            merged = {**config, **tomllib.loads(task_file.read_text(encoding="utf-8"))}
            ref = merged.get("build_image", fallback)
            add(ref, task_file)
            task_images[str(task_file.parent.relative_to(projects))] = ref
    if tasks is not None and (missing := tasks - task_images.keys()):
        raise MirrorError(f"Unknown task(s): {', '.join(sorted(missing))}")
    return {ref: sorted(paths) for ref, paths in sorted(images.items())}, task_images


def target_ref(source: str, target_repository: str, platform: str, prefix: str) -> str:
    key = hashlib.sha256(f"{source}\0{platform}".encode()).hexdigest()
    readable = re.sub(r"[^a-z0-9_.-]+", "-", source.lower()).strip(".-")
    # Hash the full source and platform; truncating the readable part is harmless.
    tag = f"{prefix}{readable[:95 - len(prefix)]}-{key[:16]}"
    return f"{target_repository}:{tag}"


@dataclass(frozen=True)
class Image:
    key: str
    source: str
    target: str
    config_files: list[str]


@dataclass(frozen=True)
class Manifest:
    digest: str
    config_digest: str
    platform: str


def parse_manifest(output: str, platform: str) -> Manifest:
    payload = json.loads(output)
    entries = payload if isinstance(payload, list) else [payload]
    requested = platform.split("/")
    matches = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        descriptor = entry.get("Descriptor", {})
        actual = descriptor.get("platform", {})
        actual_platform = "/".join(str(actual.get(p, "")) for p in ("os", "architecture"))
        variant = actual.get("variant")
        if variant:
            actual_platform += f"/{variant}"
        if actual.get("os") != requested[0] or actual.get("architecture") != requested[1]:
            continue
        if len(requested) == 3 and variant != requested[2]:
            continue
        manifest = entry.get("SchemaV2Manifest") or entry.get("OCIManifest")
        if not isinstance(manifest, dict):
            raise MirrorError("Manifest has no SchemaV2Manifest/OCIManifest; cannot verify image content")
        matches.append(Manifest(
            digest(descriptor.get("digest"), "manifest"),
            digest(manifest.get("config", {}).get("digest"), "image config"),
            actual_platform,
        ))
    if len(matches) != 1:
        raise MirrorError(f"Expected exactly one {platform} manifest, found {len(matches)}")
    return matches[0]


@contextmanager
def file_lock(path: Path, stop: threading.Event | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if stop is None:
                    raise MirrorError(f"Another mirror process owns {path}")
                if stop.wait(0.2):
                    raise Cancelled("Stopped while waiting for image lock")
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Docker:
    def __init__(self, args, stop: threading.Event):
        self.args = args
        self.stop = stop

    @staticmethod
    def terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

    def command(self, arguments: list[str], log: Path, *, timeout: int | None = None,
                missing: tuple[str, ...] = (), ignore_stop: bool = False,
                executable: str | None = None) -> str | None:
        timeout = timeout or self.args.command_timeout
        for attempt in range(1, self.args.retries + 1):
            if self.stop.is_set() and not ignore_stop:
                raise Cancelled("Run interrupted")
            log.parent.mkdir(parents=True, exist_ok=True)
            command = [executable or self.args.docker, *arguments]
            with log.open("ab", buffering=0) as stream:
                stream.write(f"\n[{now()}] $ {shlex.join(command)}\n".encode())
                offset = stream.tell()
                process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                started = time.monotonic()
                progress_at = started
                timed_out = False
                try:
                    while process.poll() is None:
                        if self.stop.is_set() and not ignore_stop:
                            raise Cancelled("Run interrupted; current Docker CLI stopped")
                        if time.monotonic() - started >= timeout:
                            timed_out = True
                            break
                        if time.monotonic() - progress_at >= 15:
                            emit(f"  {' '.join(arguments[:2])}: {arguments[-1]} still running ({int(time.monotonic() - started)}s); log={log}")
                            progress_at = time.monotonic()
                        time.sleep(0.2)
                finally:
                    self.terminate(process)
            with log.open("rb") as stream:
                size = log.stat().st_size
                stream.seek(max(offset, size - 4 * 1024 * 1024))
                output = stream.read().decode("utf-8", errors="replace")
            if process.returncode == 0 and not timed_out:
                return output
            folded = output.casefold()
            if any(marker in folded for marker in (
                "toomanyrequests", "too many requests", "pull rate limit",
            )):
                self.stop.set()
                raise RateLimited(f"Registry rate limit reached; stopping this run.\n"
                                  f"{shlex.join(command)}\n{output[-1800:]}\nLog: {log}")
            fatal = any(marker in folded for marker in FATAL_ERRORS)
            if not timed_out and not fatal and any(marker in folded for marker in missing):
                return None
            reason = f"timeout after {timeout}s" if timed_out else f"exit {process.returncode}"
            message = f"{shlex.join(command)}: {reason}\n{output[-1800:]}\nLog: {log}"
            if (not timed_out and executable is None and arguments[0] == "push"
                    and "was found but does not provide the specified platform" in folded):
                # Retrying the same pull/push cannot restore a skipped compressed blob.
                raise PushContentUnavailable(message)
            if attempt == self.args.retries or fatal:
                raise MirrorError(message)
            delay = self.args.retry_delay * min(2 ** (attempt - 1), 8)
            emit(f"  retry {attempt}/{self.args.retries}: {reason}; waiting {delay:g}s")
            if ignore_stop:
                time.sleep(delay)
            elif self.stop.wait(delay):
                raise Cancelled("Run interrupted during retry delay")
        raise AssertionError("Unreachable")

    def manifest(self, ref: str, platform: str, log: Path, *, optional=False) -> Manifest | None:
        output = self.command(["manifest", "inspect", "--verbose", ref], log,
                              timeout=self.args.probe_timeout,
                              missing=REMOTE_MISSING if optional else ())
        return None if output is None else parse_manifest(output, platform)

    def image_id(self, ref: str, log: Path, *, ignore_stop=False) -> str | None:
        output = self.command(["image", "inspect", "--format", "{{.Id}}", ref], log,
                              timeout=self.args.probe_timeout, missing=LOCAL_MISSING,
                              ignore_stop=ignore_stop)
        return None if output is None else digest(output.strip(), "local image")

    def registry_copy(self, source: str, target: str, log: Path) -> str:
        executable = getattr(self.args, "skopeo", "skopeo")
        if shutil.which(executable) is None:
            raise MirrorError("Registry copy requires skopeo; install it or set --skopeo PATH")
        platform = self.args.platform.split("/")
        options = ["--override-os", platform[0], "--override-arch", platform[1]]
        if len(platform) == 3:
            options += ["--override-variant", platform[2]]
        authfile = getattr(self.args, "skopeo_authfile", None)
        if authfile is None:
            docker_config = Path(os.getenv("DOCKER_CONFIG", str(Path.home() / ".docker")))
            if (docker_config / "config.json").is_file():
                authfile = docker_config / "config.json"
        log.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="registry-copy-", dir=log.parent) as temporary:
            digestfile = Path(temporary) / "digest"
            policy = getattr(self.args, "skopeo_policy", None)
            if policy is None:
                config_dir = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))
                policy = next((path for path in (
                    config_dir / "containers" / "policy.json", Path("/etc/containers/policy.json"),
                ) if path.is_file()), None)
            if policy is None:
                # Match Docker's unsigned-image behavior only for this exact source digest.
                # Preserve existing host policy; the temporary policy rejects other images.
                policy = Path(temporary) / "policy.json"
                atomic_json(policy, pinned_copy_policy(source))
            options += ["--policy", str(policy)]
            options += ["copy", "--preserve-digests", "--digestfile", str(digestfile)]
            if authfile is not None:
                options += ["--authfile", str(authfile)]
            # The source is already pinned to the selected platform's child manifest.
            # docker:// -> docker:// bypasses the daemon and its incomplete snapshots.
            self.command([*options, f"docker://{source}", f"docker://{target}"], log,
                         executable=executable)
            return digest(digestfile.read_text(encoding="utf-8").strip(), "registry copy")


@contextmanager
def push_slot(semaphore: threading.Semaphore, stop: threading.Event):
    while not semaphore.acquire(timeout=0.2):
        if stop.is_set():
            raise Cancelled("Stopped while waiting for push slot")
    try:
        if stop.is_set():
            raise Cancelled("Run interrupted")
        yield
    finally:
        semaphore.release()


def clean_owned_images(docker: Docker, state: dict, state_path: Path, log: Path) -> list[str]:
    errors = []
    for ref, expected in list(state["owned_local_refs"].items()):
        try:
            current = docker.image_id(ref, log, ignore_stop=True)
            if current is not None:
                # The containerd image store may expose a manifest digest as .Id.
                valid_ids = {expected, state.get("config_digest"),
                             state.get("source_manifest_digest"), state.get("target_manifest_digest")}
                if current not in valid_ids:
                    raise MirrorError(f"Local ref changed; preserving {ref}")
                docker.command(["image", "rm", ref], log,
                               timeout=docker.args.probe_timeout, ignore_stop=True)
            del state["owned_local_refs"][ref]
            atomic_json(state_path, state)
        except (MirrorError, OSError) as exc:
            errors.append(str(exc))
    return errors


def mirror_image(image: Image, args, docker: Docker, semaphore: threading.Semaphore,
                 stop: threading.Event) -> dict:
    started = time.monotonic()
    log = args.output_dir / "logs" / f"{image.key}.log"
    state_path = args.output_dir / "state" / f"{image.key}.json"
    result = {"source": image.source, "target": image.target, "status": "failed", "log": str(log)}
    # Lock the source across platforms and output directories: avoid local ref races.
    source_key = hashlib.sha256(image.source.encode()).hexdigest()
    state = {}
    locks = ExitStack()
    held_pull_keys = set()
    cleanup_pull_refs = set()
    cleanup_ready = False

    def acquire_pull_locks(cancel_event):
        keys = sorted(hashlib.sha256(ref.encode()).hexdigest() for ref in cleanup_pull_refs)
        for key in keys:
            if key not in held_pull_keys:
                locks.enter_context(file_lock(args.lock_dir / f"pull-{key}.lock", cancel_event))
                held_pull_keys.add(key)

    try:
        locks.enter_context(file_lock(args.lock_dir / f"source-{source_key}.lock", stop))
        state = {"schema_version": 1, "source": image.source, "target": image.target,
                 "platform": args.platform, "owned_local_refs": {}}
        if state_path.exists():
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            if any(previous.get(k) != state[k] for k in ("source", "target", "platform")):
                raise MirrorError(f"State does not match this image: {state_path}")
            state = previous
        cleanup_ready = True
        cleanup_pull_refs.update(ref for ref in state["owned_local_refs"] if "@sha256:" in ref)
        previous_digest = state.get("source_manifest_digest", "")
        if state["owned_local_refs"] and DIGEST_RE.fullmatch(previous_digest):
            cleanup_pull_refs.add(f"{repository_of(image.source)}@{previous_digest}")
        target = None
        cached = (state.get("status") == "verified" and not getattr(args, "refresh_source", False)
                  and all(isinstance(state.get(key), str) and DIGEST_RE.fullmatch(state[key])
                          for key in ("source_manifest_digest", "config_digest", "target_manifest_digest")))
        if cached:
            emit(f"[resume-acr] {image.source}")
            target = docker.manifest(image.target, args.platform, log, optional=True)
            if target is not None:
                if (target.digest != state["target_manifest_digest"]
                        or target.config_digest != state["config_digest"]):
                    raise MirrorError(f"ACR content differs from verified state; refusing reuse: {image.target}")
                source = Manifest(state["source_manifest_digest"], state["config_digest"], args.platform)
                result["verification_mode"] = "acr-state"
        if target is None:
            emit(f"[probe] {image.source}")
            source = docker.manifest(image.source, args.platform, log)
            result["verification_mode"] = "source-and-acr"
        result.update(source_manifest_digest=source.digest, config_digest=source.config_digest)
        state.update(source_manifest_digest=source.digest, config_digest=source.config_digest)
        immutable_source = f"{repository_of(image.source)}@{source.digest}"
        # Distinct source tags can resolve to the same pulled digest ref.
        # Keep that ref locked through tag/push/cleanup, even across runs.
        cleanup_pull_refs.add(immutable_source)
        acquire_pull_locks(stop)
        for ref in dict.fromkeys((image.source, immutable_source)):
            existing = docker.image_id(ref, log)
            if existing in {source.config_digest, source.digest}:
                state["owned_local_refs"][ref] = existing
                atomic_json(state_path, state)
        if target is None:
            target = docker.manifest(image.target, args.platform, log, optional=True)
        if target is not None:
            if target.config_digest != source.config_digest:
                raise MirrorError(f"ACR tag has different image content; refusing overwrite: {image.target}")
            existing_target = docker.image_id(image.target, log)
            if existing_target in {source.config_digest, source.digest, target.digest}:
                state["owned_local_refs"][image.target] = existing_target
                atomic_json(state_path, state)
            status = "skipped"
        else:
            backend = getattr(args, "copy_backend", "auto")
            direct_copy = backend == "skopeo" or (backend == "auto" and state.get("copy_backend") == "skopeo")
            if not direct_copy:
                current = docker.image_id(immutable_source, log)
                if current is None:
                    # Register before pulling so interrupted pulls can be cleaned on resume.
                    state["owned_local_refs"][immutable_source] = source.config_digest
                    atomic_json(state_path, state)
                    emit(f"[pull] {immutable_source}")
                    docker.command(["pull", "--platform", args.platform, immutable_source], log)
                local_id = docker.image_id(immutable_source, log)
                if local_id not in {source.config_digest, source.digest}:
                    raise MirrorError(f"Pulled image config differs from source manifest: {immutable_source}")
                if immutable_source in state["owned_local_refs"]:
                    state["owned_local_refs"][immutable_source] = local_id
                    atomic_json(state_path, state)
            current_target = docker.image_id(image.target, log)
            if current_target is not None and current_target not in {source.config_digest, source.digest}:
                raise MirrorError(f"Local ACR tag has different content; preserving it: {image.target}")
            if current_target is not None:
                state["owned_local_refs"][image.target] = current_target
                atomic_json(state_path, state)
            if not direct_copy and current_target is None:
                state["owned_local_refs"][image.target] = local_id
                atomic_json(state_path, state)
                docker.command(["tag", immutable_source, image.target], log,
                               timeout=args.probe_timeout)
            with push_slot(semaphore, stop):
                # Recheck after waiting; another publisher may have finished.
                target = docker.manifest(image.target, args.platform, log, optional=True)
                if target is not None and target.config_digest != source.config_digest:
                    raise MirrorError(f"ACR tag changed before push: {image.target}")
                if target is None:
                    pushed_digest = None
                    status = "success"
                    if not direct_copy:
                        emit(f"[push] {image.target}")
                        try:
                            output = docker.command(["push", "--platform", args.platform, image.target], log)
                            pushed = re.findall(r"\bdigest:\s*(sha256:[0-9a-f]{64})", output)
                            pushed_digest = pushed[-1] if pushed else None
                            result["copy_backend"] = "docker"
                        except PushContentUnavailable as exc:
                            if backend == "docker":
                                raise
                            result["fallback_reason"] = str(exc)
                            direct_copy = True
                    if direct_copy:
                        state["copy_backend"] = "skopeo"
                        atomic_json(state_path, state)
                        # Recheck before fallback so an independently published tag is not overwritten.
                        target = docker.manifest(image.target, args.platform, log, optional=True)
                        if target is not None:
                            if target.config_digest != source.config_digest:
                                raise MirrorError(f"ACR tag changed before registry copy: {image.target}")
                            status = "skipped"
                        else:
                            emit(f"[registry-copy] {immutable_source} -> {image.target}")
                            pushed_digest = docker.registry_copy(immutable_source, image.target, log)
                            if pushed_digest != source.digest:
                                raise MirrorError("Registry copy did not preserve source manifest digest")
                        result["copy_backend"] = "skopeo"
                    target = docker.manifest(image.target, args.platform, log)
                    if pushed_digest and pushed_digest != target.digest:
                        raise MirrorError(f"Push digest differs from remote manifest: {image.target}")
                else:
                    status = "skipped"
            if target.config_digest != source.config_digest:
                raise MirrorError(f"ACR image config differs after push: {image.target}")
        state.update(status="verified", target_manifest_digest=target.digest, verified_at=now())
        if result.get("copy_backend"):
            state["copy_backend"] = result["copy_backend"]
        atomic_json(state_path, state)
        result.update(status=status, target_manifest_digest=target.digest,
                      acr_image=f"{repository_of(image.target)}@{target.digest}")
    except RateLimited as exc:
        result.update(status="rate_limited", rate_limited=True, error=str(exc))
        stop.set()
    except Cancelled as exc:
        result.update(status="cancelled", error=str(exc))
    except Exception as exc:
        result.update(status="failed", error=str(exc))
    finally:
        # Cleanup runs before releasing source/digest locks, including on cancellation.
        try:
            if cleanup_ready:
                # Historical failed runs also get cleaned if a new source probe fails.
                # Ignore cancellation while waiting for cleanup locks and deleting refs.
                acquire_pull_locks(threading.Event())
                cleanup_errors = clean_owned_images(docker, state, state_path, log)
                result["cleanup_complete"] = not cleanup_errors
                if cleanup_errors:
                    result.update(status="failed", cleanup_errors=cleanup_errors)
                    result["error"] = (result.get("error", "") + "\nLocal cleanup failed").strip()
                    stop.set()
        except Exception as exc:
            result.update(status="failed", cleanup_complete=False, cleanup_errors=[str(exc)])
            result["error"] = (result.get("error", "") + "\nLocal cleanup failed").strip()
            stop.set()
        finally:
            locks.close()
    result["duration_seconds"] = round(time.monotonic() - started, 2)
    return result


def run_mirror(images: list[Image], args, plan: dict) -> int:
    stop = threading.Event()
    interrupted = threading.Event()
    previous_handlers = {}

    def handle_signal(signum, frame):
        interrupted.set()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, handle_signal)
    docker = Docker(args, stop)
    semaphore = threading.Semaphore(args.push_workers)
    results = []

    def checkpoint():
        counts = Counter(row["status"] for row in results)
        rate_limited = any(row.get("rate_limited") for row in results)
        atomic_json(args.output_dir / "report.json", {
            "schema_version": 1, "updated_at": now(), "platform": args.platform,
            "total_images": len(images), "completed_images": len(results),
            "pending_images": len(images) - len(results), "counts": dict(counts),
            "interrupted": interrupted.is_set(), "stopped": stop.is_set(), "images": results,
            "rate_limited": rate_limited,
            "stop_reason": "rate_limit" if rate_limited else (
                "interrupted" if interrupted.is_set() else "cleanup_failed" if stop.is_set() else None),
        })

    try:
        if shutil.which(args.docker) is None:
            raise MirrorError(f"Docker CLI not found: {args.docker}")
        if getattr(args, "copy_backend", "auto") == "skopeo" and shutil.which(args.skopeo) is None:
            raise MirrorError(f"skopeo not found: {args.skopeo}")
        docker.command(["info", "--format", "{{.ServerVersion}}"],
                       args.output_dir / "logs" / "preflight.log", timeout=args.probe_timeout)
        push_help = docker.command(["push", "--help"], args.output_dir / "logs" / "preflight.log",
                                   timeout=args.probe_timeout)
        if getattr(args, "copy_backend", "auto") != "skopeo" and "--platform" not in push_help:
            raise MirrorError("Docker CLI must support 'docker push --platform'; upgrade Docker before mirroring")
        emit(f"Starting {len(images)} images: workers={args.workers}, push_workers={args.push_workers}")
        pending = iter(images)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            inflight = {}
            try:
                while True:
                    while not stop.is_set() and len(inflight) < args.workers:
                        image = next(pending, None)
                        if image is None:
                            break
                        future = pool.submit(mirror_image, image, args, docker, semaphore, stop)
                        inflight[future] = image
                    if not inflight:
                        break
                    done, _ = wait(inflight, timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        del inflight[future]
                        result = future.result()
                        results.append(result)
                        emit(f"[{len(results)}/{len(images)} {result['status']}] {result['source']}")
                        if result.get("error"):
                            emit(f"  {result['error']}")
                        checkpoint()
            except BaseException:
                stop.set()
                for future in inflight:
                    result = future.result()
                    results.append(result)
                raise
    finally:
        try:
            checkpoint()
            verified = {r["source"]: r["acr_image"] for r in results if r["status"] in {"success", "skipped"}}
            atomic_json(args.output_dir / "image_mapping.json", {
                "schema_version": 1, "updated_at": now(), "platform": args.platform,
                "complete": len(verified) == len(images), "source_to_acr": verified,
                "task_images": {task: {"source": ref, "acr": verified.get(ref)}
                                for task, ref in plan["task_images"].items()},
            })
        finally:
            for sig, previous in previous_handlers.items():
                signal.signal(sig, previous)
    emit(f"Finished: {dict(Counter(r['status'] for r in results))}; report={args.output_dir / 'report.json'}")
    if any(row.get("rate_limited") for row in results):
        emit("Registry quota exhausted. Progress saved; rerun the same command after login/quota recovery. "
             "Verified images will be checked against ACR without querying the source registry.")
    if interrupted.is_set():
        return 130
    return 0 if len(results) == len(images) and all(r["status"] in {"success", "skipped"} for r in results) else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO)
    p.add_argument("--tasks-file", type=Path, help="Optional official project/task list; default: all configs")
    p.add_argument("--acr-registry", default=os.getenv("ACR_REGISTRY", DEFAULT_REGISTRY))
    p.add_argument("--acr-namespace", default=os.getenv("ACR_NAMESPACE", "qdyd"))
    p.add_argument("--acr-repository", default=os.getenv("ACR_REPOSITORY", "cybergym-e2e"))
    p.add_argument("--tag-prefix", default="", help="Optional prefix for a new mirror version")
    p.add_argument("--platform", default="linux/amd64", help="Single image platform; default: linux/amd64")
    p.add_argument("--workers", type=int, default=4, help="Max images in flight (pull/probe/push/cleanup)")
    p.add_argument("--push-workers", type=int, default=2, help="Independent limit for concurrent pushes")
    p.add_argument("--retries", type=int, default=3, help="Max attempts for a Docker/skopeo command")
    p.add_argument("--retry-delay", type=float, default=10, help="Initial exponential backoff in seconds")
    p.add_argument("--command-timeout", type=int, default=7200, help="Pull/push timeout in seconds")
    p.add_argument("--probe-timeout", type=int, default=120, help="Manifest/inspect/tag/cleanup timeout")
    p.add_argument("--output-dir", type=Path, default=HERE / "runs" / "default")
    p.add_argument("--lock-dir", type=Path, default=Path(tempfile.gettempdir()) / "cybergym-e2e-acr-locks")
    p.add_argument("--docker", default="docker", help="Docker CLI executable")
    p.add_argument("--copy-backend", choices=("auto", "docker", "skopeo"), default="auto",
                   help="auto: Docker with skopeo fallback for missing local platform content")
    p.add_argument("--skopeo", default="skopeo", help="skopeo executable for registry-to-registry copying")
    p.add_argument("--skopeo-authfile", type=Path,
                   help="Registry copy credentials file; default: Docker config.json when present")
    p.add_argument("--skopeo-policy", type=Path,
                   help="Signature policy file; default: host policy or temporary policy for the pinned source")
    p.add_argument("--limit", type=int, help="Only process the first N sorted unique images")
    p.add_argument("--refresh-source", action="store_true",
                   help="Recheck upstream for verified images too (uses source registry quota)")
    p.add_argument("--dry-run", action="store_true", help="Write plan.json without Docker/network calls")
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    for name in ("workers", "push_workers", "retries", "command_timeout", "probe_timeout"):
        if getattr(args, name) <= 0:
            p.error(f"--{name.replace('_', '-')} must be positive")
    if args.limit is not None and args.limit <= 0:
        p.error("--limit must be positive")
    if args.retry_delay < 0:
        p.error("--retry-delay must be non-negative")
    if not re.fullmatch(r"[a-z0-9_]+/[a-z0-9_]+(?:/[a-z0-9_.-]+)?", args.platform):
        p.error("--platform must be os/architecture[/variant]")
    if len(args.tag_prefix) > 32 or (args.tag_prefix and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", args.tag_prefix)):
        p.error("--tag-prefix must start with a-z/0-9 and contain at most 32 lowercase tag characters")
    registry = args.acr_registry.strip().strip("/")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]+)?", registry):
        p.error("--acr-registry must be a hostname[:port], without scheme/path")
    for name in ("acr_namespace", "acr_repository"):
        if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", getattr(args, name)):
            p.error(f"Invalid --{name.replace('_', '-')}")
    args.repo_dir = args.repo_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    args.lock_dir = args.lock_dir.resolve()
    try:
        refs, task_images = collect_images(args.repo_dir, read_tasks(args.tasks_file))
        target_repository = f"{registry}/{args.acr_namespace}/{args.acr_repository}"
        images = []
        for source, config_files in refs.items():
            target = target_ref(source, target_repository, args.platform, args.tag_prefix)
            key = hashlib.sha256(f"{source}\0{target}\0{args.platform}".encode()).hexdigest()
            images.append(Image(key, source, target, config_files))
        if args.limit is not None:
            images = images[:args.limit]
        if len({image.target for image in images}) != len(images):
            raise MirrorError("Target tag collision detected")
        selected_refs = {image.source for image in images}
        plan = {"schema_version": 1, "created_at": now(), "repo_dir": str(args.repo_dir),
                "target_repository": target_repository, "platform": args.platform,
                "total_unique_images": len(refs), "selected_images": len(images),
                "task_images": {t: r for t, r in task_images.items() if r in selected_refs},
                "images": [asdict(image) for image in images]}
        with file_lock(args.output_dir / ".run.lock"):
            atomic_json(args.output_dir / "plan.json", plan)
            emit(f"Found {len(task_images)} tasks, {len(refs)} unique images; selected={len(images)}")
            emit(f"Target: {target_repository}; platform={args.platform}")
            if args.dry_run:
                for image in images[:10]:
                    emit(f"  {image.source}\n    -> {image.target}")
                emit(f"Dry run: no Docker/network calls. Full plan: {args.output_dir / 'plan.json'}")
                return 0
            return run_mirror(images, args, plan)
    except (MirrorError, OSError, ValueError, SyntaxError) as exc:
        emit(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
