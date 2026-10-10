"""manifest：任务筛选、build_image 合并、ACR 替换与续跑核对。"""

import json
import tempfile
import unittest
from pathlib import Path

from shelllib import manifest as manifest_mod
from tests.helpers import FAKE_IMAGE_MAPPING, make_config, make_fake_dataset


def _prepare(tmp: Path):
    make_fake_dataset(tmp)
    cfg = make_config(tmp)
    return cfg


class SelectTasksTests(unittest.TestCase):
    def test_select_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _prepare(Path(tmp))
            tasks, dropped = manifest_mod.select_tasks(cfg)
            self.assertEqual(tasks, ["curl/arvo_66012"])
            self.assertEqual(dropped["official_total"], 1)

    def test_exclude_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_fake_dataset(tmp_path)
            cfg = make_config(
                tmp_path,
                exclude_completed_task_file=_write(tmp_path, "curl/arvo_66012\n"),
            )
            with self.assertRaises(manifest_mod.FreezeError):
                manifest_mod.select_tasks(cfg)

    def test_missing_assets_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = make_config(tmp_path)  # 没有下载数据
            with self.assertRaises(manifest_mod.FreezeError):
                manifest_mod.select_tasks(cfg)


class MergeBuildImageTests(unittest.TestCase):
    def test_task_override_beats_project_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = _prepare(tmp_path)
            # 任务 config 覆盖 build_image
            task_config = cfg.repo_dir / "projects" / "curl" / "arvo_66012" / "config.toml"
            task_config.write_text(
                task_config.read_text() + 'build_image = "n132/arvo:66012-fix"\n'
            )
            source, config = manifest_mod.merge_build_image(cfg.repo_dir, "curl/arvo_66012")
            self.assertEqual(source, "n132/arvo:66012-fix")
            self.assertEqual(config["repo_to_patch"], "curl")

    def test_project_default_used_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = _prepare(tmp_path)
            source, config = manifest_mod.merge_build_image(cfg.repo_dir, "curl/arvo_66012")
            self.assertEqual(source, "gcr.io/oss-fuzz-base/base-builder@sha256:aaa")
            self.assertEqual(config["immutable_files"], ["tests/"])

    def test_fallback_when_both_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = _prepare(tmp_path)
            project_toml = cfg.repo_dir / "projects" / "curl" / "project.toml"
            project_toml.write_text('repo_to_patch = "curl"\n')  # 去掉 build_image
            source, _ = manifest_mod.merge_build_image(cfg.repo_dir, "curl/arvo_66012")
            self.assertEqual(source, manifest_mod.DEFAULT_BUILD_IMAGE)


class AcrResolutionTests(unittest.TestCase):
    def test_task_exact_match_preferred(self):
        reference = manifest_mod.resolve_acr_reference(
            "curl/arvo_66012", "gcr.io/oss-fuzz-base/base-builder@sha256:aaa", FAKE_IMAGE_MAPPING
        )
        self.assertEqual(reference, "reg.example.com/e2e/img@sha256:bbb")

    def test_source_fallback(self):
        reference = manifest_mod.resolve_acr_reference(
            "curl/other_task", "gcr.io/oss-fuzz-base/base-builder@sha256:aaa", FAKE_IMAGE_MAPPING
        )
        self.assertEqual(reference, "reg.example.com/e2e/img@sha256:aaa")

    def test_missing_mapping_raises(self):
        with self.assertRaises(manifest_mod.FreezeError):
            manifest_mod.resolve_acr_reference("x/y", "unknown/image:tag", FAKE_IMAGE_MAPPING)


class FreezeAndResumeTests(unittest.TestCase):
    def test_freeze_writes_manifest_and_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _prepare(Path(tmp))
            cfg.derive()
            payload = manifest_mod.prepare_new_experiment(cfg)
            self.assertTrue(cfg.manifest_path().exists())
            self.assertEqual(len(payload["tasks"]), 1)
            entry = payload["tasks"][0]
            self.assertEqual(entry["build_image_acr"], "reg.example.com/e2e/img@sha256:bbb")
            self.assertIn("src.tgz", entry["data_files"])
            # 冻结文件存在
            frozen_data = cfg.frozen_dir / "dataset" / "curl" / "arvo_66012" / "src.tgz"
            self.assertTrue(frozen_data.exists())

    def test_double_freeze_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _prepare(Path(tmp))
            cfg.derive()
            manifest_mod.prepare_new_experiment(cfg)
            with self.assertRaises(manifest_mod.FreezeError):
                manifest_mod.prepare_new_experiment(cfg)

    def test_resume_verification_detects_config_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = _prepare(tmp_path)
            cfg.derive()
            manifest_mod.prepare_new_experiment(cfg)
            # 改变核心参数（模型）后续跑应被拒绝
            from shelllib.config import StagePolicy

            cfg2 = make_config(tmp_path, model="glm54", stage_policy=StagePolicy(
                mode="e2e", enabled_stages=[1, 2, 3, 4], success_stages=[1, 2, 3]))
            with self.assertRaises(manifest_mod.FreezeError):
                manifest_mod.verify_manifest_for_resume(cfg2)
            # 未改变时通过
            report = manifest_mod.verify_manifest_for_resume(cfg)
            self.assertTrue(report["verified"])

    def test_resume_verification_detects_asset_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = _prepare(tmp_path)
            cfg.derive()
            manifest_mod.prepare_new_experiment(cfg)
            # 篡改冻结资产（模拟硬链接源被改）
            frozen_poc = cfg.frozen_dir / "dataset" / "curl" / "arvo_66012" / "poc.bin"
            frozen_poc.write_bytes(b"tampered")
            with self.assertRaises(manifest_mod.FreezeError):
                manifest_mod.verify_manifest_for_resume(cfg)

    def test_manifest_has_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = _prepare(tmp_path)
            cfg.derive()
            payload = manifest_mod.prepare_new_experiment(cfg)
            text = json.dumps(payload)
            self.assertNotIn("sk-", text)
            for key in payload:
                self.assertNotIn("api_key", key.lower())


def _write(directory: Path, text: str) -> Path:
    path = directory / "exclude.txt"
    path.write_text(text, encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
