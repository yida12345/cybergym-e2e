#!/usr/bin/env python3
"""Calculate registry image sizes offline from verified mirror manifest logs."""

import argparse
import base64
import csv
import hashlib
import json
from pathlib import Path
import re

from mirror_images import HERE, MirrorError, atomic_json, digest


def logged_manifest(path, wanted):
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    for start in re.finditer(r"(?m)^[\[{]\s*$", text):
        try:
            payload, _ = decoder.raw_decode(text[start.start():])
        except json.JSONDecodeError:
            continue
        for entry in payload if isinstance(payload, list) else [payload]:
            if not isinstance(entry, dict) or entry.get("Descriptor", {}).get("digest") != wanted:
                continue
            if not isinstance(entry.get("Raw"), str):
                continue
            raw = base64.b64decode(entry["Raw"], validate=True)
            if "sha256:" + hashlib.sha256(raw).hexdigest() != wanted:
                raise MirrorError(f"Logged manifest digest mismatch: {path}")
            manifest = json.loads(raw)
            if isinstance(manifest.get("layers"), list) and isinstance(manifest.get("config"), dict):
                return manifest, len(raw)
    raise MirrorError(f"No verified manifest for {wanted} in {path}; rerun mirror to refresh logs")


def add_blob(blobs, descriptor):
    key = digest(descriptor.get("digest"), "blob")
    size = descriptor.get("size")
    if type(size) is not int or size < 0:
        raise MirrorError(f"Invalid blob size: {key}: {size!r}")
    if key in blobs and blobs[key] != size:
        raise MirrorError(f"Conflicting sizes for {key}")
    blobs[key] = size
    return size


def statistics(run_dir):
    mapping = json.loads((run_dir / "image_mapping.json").read_text())
    if not mapping.get("complete"):
        raise MirrorError("Mirror mapping is incomplete; finish mirroring before measuring the full dataset")
    report = json.loads((run_dir / "report.json").read_text())
    records = {r["source"]: r for r in report["images"] if r["status"] in {"success", "skipped"}}
    layers, configs, manifests, all_blobs = {}, {}, {}, {}
    rows = []
    for source, target in sorted(mapping["source_to_acr"].items()):
        wanted = digest(target.rsplit("@", 1)[-1], "ACR manifest")
        row = records.get(source, {})
        if row.get("target_manifest_digest") != wanted:
            raise MirrorError(f"Report and mapping do not match: {source}")
        manifest, manifest_size = logged_manifest(Path(row["log"]), wanted)
        if manifest["config"].get("digest") != row["config_digest"]:
            raise MirrorError(f"Config digest mismatch: {source}")
        layer_bytes = sum(add_blob(layers, blob) for blob in manifest["layers"])
        config_bytes = add_blob(configs, manifest["config"])
        descriptor = {"digest": wanted, "size": manifest_size}
        add_blob(manifests, descriptor)
        for blob in [*manifest["layers"], manifest["config"], descriptor]:
            add_blob(all_blobs, blob)
        total = layer_bytes + config_bytes + manifest_size
        rows.append(dict(source=source, acr_image=target, layer_count=len(manifest["layers"]),
                         layers_bytes=layer_bytes, config_bytes=config_bytes,
                         manifest_bytes=manifest_size, total_bytes=total,
                         total_gib=round(total / 2**30, 6)))
    summed = sum(row["total_bytes"] for row in rows)
    unique = sum(all_blobs.values())
    summary = dict(
        schema_version=1, method="offline SHA256-verified ACR manifests from mirror logs",
        platform=mapping["platform"], image_references=len(rows),
        unique_manifests=len(manifests), task_count=len(mapping["task_images"]),
        summed_image_bytes=summed, unique_blob_bytes=unique,
        summed_image_tb=summed / 10**12, unique_blob_tb=unique / 10**12,
        summed_image_gib=summed / 2**30, unique_blob_gib=unique / 2**30,
        shared_bytes_saved=summed - unique, unique_layers=len(layers),
        unique_layer_bytes=sum(layers.values()), unique_configs=len(configs),
        unique_config_bytes=sum(configs.values()), unique_manifest_bytes=sum(manifests.values()),
        unique_blobs=len(all_blobs),
        limitations="Registry-encoded bytes only; excludes registry overhead, signatures, other tags, "
                     "and uncompressed local snapshots. Not an ACR billing or local disk measurement.",
    )
    return summary, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=HERE / "runs/default")
    parser.add_argument("--output-dir", type=Path, help="Default: RUN_DIR/size_stats")
    args = parser.parse_args()
    output_dir = args.output_dir or args.run_dir / "size_stats"
    summary, rows = statistics(args.run_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(output_dir / "summary.json", summary)
    with (output_dir / "images.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["total_bytes"], reverse=True))
    lines = ["# CyberGym-E2E 镜像大小统计", "",
             f"统计范围：{len(rows)} 个源镜像引用，{summary['unique_manifests']} 个不同 manifest，"
             f"对应 {summary['task_count']} 个任务；平台 {summary['platform']}。",
             "使用已有日志中的 ACR manifest，按 SHA256 校验，不访问网络或下载镜像。", "",
             "| 统计口径 | 十进制 TB | 二进制 GiB | 字节 |", "| --- | ---: | ---: | ---: |",
             f"| 逐镜像相加，重复层重复计入 | {summary['summed_image_tb']:.4f} | "
             f"{summary['summed_image_gib']:.2f} | {summary['summed_image_bytes']:,} |",
             f"| 所有 layer/config/manifest 按 digest 去重 | {summary['unique_blob_tb']:.4f} | "
             f"{summary['unique_blob_gib']:.2f} | {summary['unique_blob_bytes']:,} |", "",
             "这是仓库中的压缩数据量，不包含仓库额外开销、签名和清单外的其他镜像。",
             "本地运行还需解压后的镜像层和容器空间，不能直接按此数值准备磁盘。",
             "实际 ACR 存储计量也可能不同。", "", "## 最大的 10 个镜像", "",
             "| 源镜像 | GiB |", "| --- | ---: |"]
    for row in sorted(rows, key=lambda r: r["total_bytes"], reverse=True)[:10]:
        lines.append(f"| `{row['source']}` | {row['total_gib']:.2f} |")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Reports: {output_dir.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except (MirrorError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
