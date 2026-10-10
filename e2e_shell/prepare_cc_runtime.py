#!/usr/bin/env python3
"""独立准备并核验 CC 2.1.91 共享运行时。

- 下载 Node 20（linux x64）到 output/tools/cc-runtime/node/；
- 以固定版本安装 @anthropic-ai/claude-code@<version>；
- 生成 bin/claude 包装脚本（容器内以 /tools 只读挂载使用）；
- 记录版本与 SHA-256 到 manifest.json；
- 不修改 /data/exploitgym/data/runtime（那是 ExploitGym 的 2.1.119）。

需要网络（nodejs.org 与 registry.npmjs.org）。工具目录只读挂载进容器，
CC 会话目录按 attempt 独立可写（由 cc_runner 设置 CLAUDE_CONFIG_DIR）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_NODE_VERSION = "20.18.1"
NODE_DIST_BASE = "https://nodejs.org/dist"
NPM_REGISTRY = "https://registry.npmjs.org"

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {url} -> {destination}")
    with urllib.request.urlopen(url, timeout=600) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)

def prepare_node(root: Path, node_version: str) -> dict:
    node_dir = root / "node"
    marker = node_dir / ".prepared"
    if marker.exists():
        version = marker.read_text().strip()
        return {"node_version": version, "path": str(node_dir), "reused": True}

    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        raise RuntimeError(f"不支持的架构: {machine}")
    archive_name = f"node-v{node_version}-linux-{arch}.tar.xz"
    url = f"{NODE_DIST_BASE}/v{node_version}/{archive_name}"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / archive_name
        download(url, archive)
        extract_root = Path(tmp) / "extract"
        extract_root.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(extract_root, filter="data")
        inner = next(extract_root.iterdir())
        if node_dir.exists():
            shutil.rmtree(node_dir)
        shutil.copytree(inner, node_dir)
    # 记录二进制指纹（架构/动态库兼容性在容器内安装时检查）
    node_bin = node_dir / "bin" / "node"
    if not node_bin.exists():
        raise RuntimeError("解包后没有 bin/node")
    marker.write_text(node_version)
    return {
        "node_version": node_version,
        "node_sha256": sha256_file(node_bin),
        "path": str(node_dir),
        "reused": False,
    }

def install_claude_code(root: Path, version: str, node_dir: Path) -> dict:
    package_dir = root / "claude-code"
    marker = package_dir / ".prepared"
    if marker.exists() and marker.read_text().strip() == version:
        return {"claude_code_version": version, "path": str(package_dir), "reused": True}

    npm = node_dir / "bin" / "npm"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # 用 NPM_CONFIG_PREFIX 安装到固定目录，不污染全局
        result = subprocess.run(
            [str(npm), "install", "--no-audit", "--no-fund",
             f"@anthropic-ai/claude-code@{version}", "--prefix", str(tmp_path)],
            capture_output=True, text=True, timeout=1800,
            env={**__import__("os").environ, "npm_config_registry": NPM_REGISTRY},
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"npm install @anthropic-ai/claude-code@{version} 失败: {result.stderr[-800:]}"
            )
        installed = tmp_path / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
        if not installed.exists():
            raise RuntimeError("安装目录未生成")
        if package_dir.exists():
            shutil.rmtree(package_dir)
        shutil.copytree(installed, package_dir)
    package_json = package_dir / "package.json"
    actual = json.loads(package_json.read_text())["version"]
    if actual != version:
        raise RuntimeError(f"安装版本不符: 期望 {version}，实际 {actual}")
    marker.write_text(version)
    return {
        "claude_code_version": version,
        "claude_code_sha256": sha256_file(package_json),
        "path": str(package_dir),
        "reused": False,
    }

def write_claude_wrapper(root: Path) -> Path:
    """生成 bin/claude：容器内以 /tools 挂载后可直接调用。

    CC 的入口随版本变化：旧版是 cli.js（用 node 执行），新版 bin/claude.exe
    是原生 ELF。包装脚本按候选路径与内容判定，与 ExploitGym 的
    claude-code.sh 同思路，但指向本 runtime 目录，不借用其工具树。
    """
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "claude"
    wrapper.write_text(
        "#!/bin/sh\n"
        '# CC 共享 runtime 包装：$CC_RUNTIME_ROOT 由调用方环境注入（默认 /tools）。\n'
        'CC_RUNTIME_ROOT="${CC_RUNTIME_ROOT:-/tools}"\n'
        'PKG="$CC_RUNTIME_ROOT/claude-code"\n'
        'TARGET=""\n'
        'for candidate in "$PKG/bin/claude.exe" "$PKG/cli.js" "$PKG/bin/claude"; do\n'
        '  if [ -e "$candidate" ]; then TARGET="$candidate"; break; fi\n'
        'done\n'
        '[ -n "$TARGET" ] || { echo "claude entry not found in $PKG" >&2; exit 127; }\n'
        'if head -c 4 "$TARGET" 2>/dev/null | grep -q "$(printf \\"\\\\177ELF\\")"; then\n'
        '  exec "$TARGET" "$@"\n'
        'else\n'
        '  exec "$CC_RUNTIME_ROOT/node/bin/node" "$TARGET" "$@"\n'
        'fi\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper

def verify(root: Path) -> dict:
    """核验已准备的 runtime；返回 manifest 内容。"""
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"runtime 未准备: {root}")
    manifest = json.loads(manifest_path.read_text())
    node_bin = root / "node" / "bin" / "node"
    if not node_bin.exists():
        raise RuntimeError("node 二进制缺失")
    package_json = root / "claude-code" / "package.json"
    if not package_json.exists():
        raise RuntimeError("claude-code 包缺失")
    payload = json.loads(package_json.read_text())
    if payload["version"] != manifest["claude_code"]["version"]:
        raise RuntimeError("claude-code 版本与 manifest 不符")
    return manifest

def main() -> int:
    parser = argparse.ArgumentParser(description="准备 CC 共享 runtime")
    parser.add_argument("--version", default="2.1.91",
                        help="Claude Code 版本（固定为官方 E2E 使用的 2.1.91）")
    parser.add_argument("--node-version", default=DEFAULT_NODE_VERSION)
    parser.add_argument("--root", default="/data/cybergym-e2e/output/tools/cc-runtime")
    args = parser.parse_args()

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    node_info = prepare_node(root, args.node_version)
    package_info = install_claude_code(root, args.version, root / "node")
    wrapper = write_claude_wrapper(root)
    manifest = {
        "schema_version": 1,
        "node": node_info,
        "claude_code": package_info,
        "wrapper": str(wrapper),
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    verify(root)
    print(json.dumps({"prepared": True, **manifest}, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
