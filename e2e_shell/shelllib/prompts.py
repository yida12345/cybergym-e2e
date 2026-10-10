"""官方提示的显式加载：从冻结版 run_agent.py 提取 prompt 函数。

不复制提示文本、不做运行时文本替换：用 AST 从官方源码中提取
get_prompt_iterative / get_prompt_no_test / get_prompt 三个函数，在受控
命名空间中执行。官方代码版本与关键函数 hash 记录在 manifest（见
config.repo_script_fingerprints）；仓库更新导致接口不匹配时启动前报错，
避免静默误用。
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

PROMPT_FUNCTION_NAMES = ("get_prompt_iterative", "get_prompt_no_test", "get_prompt")


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptSource:
    repo_dir: Path
    function_hashes: dict[str, str]


class _ArgsLike(Protocol):
    prompt_style: str
    mode: str


def _extract_functions(source: str) -> tuple[dict[str, ast.FunctionDef], dict[str, str]]:
    """从源码 AST 中摘出 prompt 函数；返回 (节点, 源码 hash)。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PromptError(f"官方 run_agent.py 解析失败: {exc}") from exc
    nodes: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in PROMPT_FUNCTION_NAMES:
            nodes[node.name] = node
    missing = [name for name in PROMPT_FUNCTION_NAMES if name not in nodes]
    if missing:
        raise PromptError(f"官方 run_agent.py 缺少 prompt 函数: {missing}")
    hashes = {
        name: hashlib.sha256(ast.unparse(nodes[name]).encode()).hexdigest()
        for name in nodes
    }
    return nodes, hashes


def load_prompt_functions(repo_dir: Path) -> tuple[Any, dict[str, str]]:
    """加载官方 prompt 函数；返回 (带函数的命名空间, 函数 hash)。"""
    run_agent_path = Path(repo_dir) / "scripts" / "run_agent.py"
    if not run_agent_path.exists():
        raise PromptError(f"官方 run_agent.py 不存在: {run_agent_path}")
    source = run_agent_path.read_text(encoding="utf-8")
    nodes, hashes = _extract_functions(source)

    module = ast.parse(source)
    keep = [node for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in PROMPT_FUNCTION_NAMES]
    loader = ast.Module(body=keep, type_ignores=[])
    namespace: dict[str, Any] = {"Path": Path, "get_poc_hex_dump": _no_poc_hex_dump}
    exec(compile(loader, filename=str(run_agent_path), mode="exec"), namespace)  # noqa: S102 - 显式、可审计的官方源码提取
    for name in PROMPT_FUNCTION_NAMES:
        if not callable(namespace.get(name)):
            raise PromptError(f"提取的函数不可调用: {name}")
    return namespace, hashes


def _no_poc_hex_dump(poc_file, max_bytes=200) -> str:  # pragma: no cover - feedback 路径，外壳不用
    """feedback-only 依赖占位；外壳单次执行（max_attempts=1）不调用。"""
    return f"<poc omitted: {poc_file}>"


class OfficialPrompts:
    """官方提示的稳定包装；启动时校验 hash。"""

    def __init__(self, repo_dir: Path, expected_hashes: dict[str, str] | None = None):
        self.namespace, self.hashes = load_prompt_functions(repo_dir)
        if expected_hashes:
            mismatched = {
                name: (expected, self.hashes.get(name))
                for name, expected in expected_hashes.items()
                if self.hashes.get(name) != expected
            }
            if mismatched:
                raise PromptError(
                    f"官方 prompt 函数 hash 与冻结值不匹配: {mismatched}；"
                    "请核对官方仓库版本（新实验需重新冻结输入）"
                )

    def build_prompt(self, *, mode: str, repo_dir: str, repo_to_patch: str,
                     prompt_style: str, feedback: str = "") -> str:
        """与官方 get_prompt(args, ...) 相同的调用语义。"""
        args = _SimpleArgs(prompt_style=prompt_style, mode=mode)
        if prompt_style == "iterative":
            return self.namespace["get_prompt_iterative"](mode, repo_to_patch, feedback)
        return self.namespace["get_prompt_no_test"](mode, repo_dir, feedback)


@dataclass
class _SimpleArgs:
    prompt_style: str
    mode: str
