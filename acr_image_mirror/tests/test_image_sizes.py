import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import image_sizes


class ImageSizeTests(unittest.TestCase):
    def test_shared_layer_and_config_are_counted_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"digest": "sha256:" + "a" * 64, "size": 50}
            layer = {"digest": "sha256:" + "b" * 64, "size": 1000}
            mapping, records, manifest_bytes = {}, [], 0
            for index in range(2):
                raw = json.dumps({"config": config, "layers": [layer], "annotations": {"index": index}}).encode()
                wanted = "sha256:" + hashlib.sha256(raw).hexdigest()
                entry = {"Descriptor": {"digest": wanted}, "Raw": base64.b64encode(raw).decode()}
                log = root / f"{index}.log"
                log.write_text("command output\n" + json.dumps(entry, indent=2) + "\n")
                source = f"source/image:{index}"
                mapping[source] = "acr.example/repo@" + wanted
                records.append(dict(source=source, status="success", log=str(log),
                                    target_manifest_digest=wanted, config_digest=config["digest"]))
                manifest_bytes += len(raw)
            (root / "image_mapping.json").write_text(json.dumps(dict(
                complete=True, platform="linux/amd64", source_to_acr=mapping, task_images={"task": {}})))
            (root / "report.json").write_text(json.dumps({"images": records}))
            summary, rows = image_sizes.statistics(root)
            self.assertEqual(len(rows), 2)
            self.assertEqual(summary["summed_image_bytes"], 2100 + manifest_bytes)
            self.assertEqual(summary["unique_blob_bytes"], 1050 + manifest_bytes)
            self.assertEqual(summary["unique_layers"], 1)
            self.assertEqual(summary["shared_bytes_saved"], 1050)

    def test_changed_manifest_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "manifest.log"
            wanted = "sha256:" + "c" * 64
            log.write_text(json.dumps({"Descriptor": {"digest": wanted},
                                      "Raw": base64.b64encode(b"{}").decode()}, indent=2))
            with self.assertRaisesRegex(image_sizes.MirrorError, "digest mismatch"):
                image_sizes.logged_manifest(log, wanted)

    def test_same_digest_with_conflicting_size_is_rejected(self):
        blobs = {}
        key = "sha256:" + "d" * 64
        image_sizes.add_blob(blobs, {"digest": key, "size": 10})
        with self.assertRaisesRegex(image_sizes.MirrorError, "Conflicting sizes"):
            image_sizes.add_blob(blobs, {"digest": key, "size": 11})


if __name__ == "__main__":
    unittest.main()
