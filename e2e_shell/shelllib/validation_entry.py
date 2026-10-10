"""容器内薄入口：调用冻结版官方 validate_task 并保存完整返回值。

该文件会被复制到容器的 /scripts/validation_entry.py，用镜像内
/scripts/.venv/bin/python 执行。它不重写通过条件，只做三件事：

1. 以 only_stage=N、run_prepare=False 调用官方 validate_task
   （可信 prepare 已由外壳提前完成，不重复 --run-prepare）；
2. 把包含 status/output/description 的完整返回值写入 JSON；
3. 记录容器内证据路径，供 host 侧保存。

官方原生 validate.py 的 main() 只落最小状态集合，无法保留 output 与
description；此入口是官方裁判的薄包装，不改变判定算法。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="官方 validate_task 薄入口")
    parser.add_argument("--only-stage", type=int, choices=[1, 2, 3, 4], required=True)
    parser.add_argument("--patch-file", default=None)
    parser.add_argument("--poc-file", default=None)
    parser.add_argument("--src-dir", default="/src")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--config-dir", default="/config")
    parser.add_argument("--skip-stage4", action="store_true", default=False)
    parser.add_argument("--json-output", required=True)
    args = parser.parse_args()

    scripts_dir = Path("/scripts")
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import validate  # 官方冻结版本（外壳已复制到容器内）

    poc_path = Path(args.poc_file) if args.poc_file else None
    if poc_path is not None and not poc_path.exists():
        poc_path = None
    patch_path = Path(args.patch_file) if args.patch_file else None
    if patch_path is not None and not patch_path.exists():
        patch_path = None

    results = validate.validate_task(
        patch_path=patch_path,
        poc_path=poc_path,
        src_dir=args.src_dir,
        data_dir=args.data_dir,
        config_dir=args.config_dir,
        run_prepare=False,  # 可信 prepare 已提前完成
        skip_stage4=args.skip_stage4,
        only_stage=args.only_stage,
        verbose=True,
    )

    payload = {
        "schema_version": 1,
        "only_stage": args.only_stage,
        "inputs": {
            "patch_file": str(patch_path) if patch_path else None,
            "poc_file": str(poc_path) if poc_path else None,
        },
        "results": {
            f"stage{stage}": {
                "status": value.get("status") if isinstance(value, dict) else value,
                "output": value.get("output") if isinstance(value, dict) else "",
                "description": value.get("description") if isinstance(value, dict) else "",
            }
            for stage, value in (
                (1, results.get("stage1")),
                (2, results.get("stage2")),
                (3, results.get("stage3")),
                (4, results.get("stage4")),
            )
        },
        "frozen_validate_sha256": _file_hash(scripts_dir / "validate.py"),
    }
    output_path = Path(args.json_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # stdout 留给 host 持续采集的验证日志。
    status = payload["results"][f"stage{args.only_stage}"]["status"]
    print(f"VALIDATION_ENTRY stage={args.only_stage} status={status}")
    return 0


def _file_hash(path: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
