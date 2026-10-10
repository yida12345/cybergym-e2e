"""配置、阶段解析与子集约束、模式规则与秘密不落盘。"""

import json
import os
import unittest
from pathlib import Path

from shelllib import util
from shelllib.config import (
    ConfigError,
    DEFAULT_STAGES,
    resolve_stages,
    scrub_secrets,
)


class StageParsingTests(unittest.TestCase):
    def test_auto_resolves_per_mode(self):
        enabled, success = resolve_stages("e2e", "auto", "auto")
        self.assertEqual(enabled, DEFAULT_STAGES["e2e"][0])
        self.assertEqual(success, DEFAULT_STAGES["e2e"][1])
        enabled, success = resolve_stages("patch-only", "auto", "auto")
        self.assertEqual(enabled, [3, 4])
        self.assertEqual(success, [3, 4])

    def test_space_or_comma_separated_dedupe_and_sort(self):
        enabled, success = resolve_stages("e2e", "3,1,4 1", "1 3")
        self.assertEqual(enabled, [1, 3, 4])
        self.assertEqual(success, [1, 3])

    def test_success_must_be_subset_of_enabled(self):
        with self.assertRaises(ConfigError):
            resolve_stages("e2e", "1 2", "1 3")

    def test_success_must_be_nonempty(self):
        with self.assertRaises(ConfigError):
            resolve_stages("e2e", "1 2 3 4", "")

    def test_patch_only_rejects_s1_s2(self):
        with self.assertRaises(ConfigError):
            resolve_stages("patch-only", "1 2 3 4", "3 4")
        with self.assertRaises(ConfigError):
            resolve_stages("patch-only", "3 4", "3 4 2")
        # 合法
        resolve_stages("patch-only", "3 4", "3")

    def test_unknown_stage_rejected(self):
        with self.assertRaises(ConfigError):
            resolve_stages("e2e", "1 5", "1")
        with self.assertRaises(ConfigError):
            resolve_stages("e2e", "abc", "1")

    def test_invalid_mode(self):
        with self.assertRaises(ConfigError):
            resolve_stages("other", "auto", "auto")


class ScrubSecretsTests(unittest.TestCase):
    def test_secrets_redacted(self):
        cleaned = scrub_secrets({
            "model": "glm53",
            "api_key": "sk-secret",
            "ANTHROPIC_API_KEY": "sk-x",
            "nested": {"token": "abc", "ok": "value"},
            "items": [{"password": "p"}, 1],
        })
        self.assertEqual(cleaned["model"], "glm53")
        self.assertEqual(cleaned["api_key"], "<redacted>")
        self.assertEqual(cleaned["ANTHROPIC_API_KEY"], "<redacted>")
        self.assertEqual(cleaned["nested"]["token"], "<redacted>")
        self.assertEqual(cleaned["nested"]["ok"], "value")
        self.assertEqual(cleaned["items"][0]["password"], "<redacted>")
        self.assertEqual(cleaned["items"][1], 1)


class LoadConfigTests(unittest.TestCase):
    def _env(self, **extra):
        env = {
            "EXP": "exp_test", "ROUND_NUMBER": "1", "MODE": "e2e",
            "ENABLED_STAGES": "auto", "SUCCESS_STAGES": "auto",
            "ANTHROPIC_API_KEY": "sk-test",
        }
        env.update(extra)
        return env

    def _load(self, env, tmp):
        repo = tmp / "repo"
        (repo / "scripts").mkdir(parents=True)
        for name in ("run_agent.py", "utils.py", "validate.py"):
            (repo / "scripts" / name).write_text("# stub\n")
        (repo / "scripts" / "install_validate_deps.sh").write_text("# stub\n")
        (repo / "projects" / "curl").mkdir(parents=True)
        (repo / "scripts" / "tasks.txt").write_text("")
        mapping = tmp / "mapping.json"
        mapping.write_text(json.dumps({"schema_version": 1, "complete": True, "source_to_acr": {}}))
        env.update({
            "OUTPUT_ROOT": str(tmp / "output"),
            "REPO_DIR": str(repo),
            "DATASET_DIR": str(tmp / "dataset"),
            "IMAGE_MAPPING_FILE": str(mapping),
            "SOURCE_TASK_FILE": str(repo / "scripts" / "tasks.txt"),
        })
        for key, value in env.items():
            os.environ[key] = str(value)
        from shelllib.config import load_config_from_env

        return load_config_from_env()

    def test_exp_directory_traversal_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self.assertRaises(ConfigError):
                self._load(self._env(EXP="../bad"), tmp_path)

    def test_custom_profile_flag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = self._load(self._env(ENABLED_STAGES="3 4", SUCCESS_STAGES="3"), tmp_path)
            self.assertTrue(cfg.stage_policy.is_custom_profile)
            self.assertEqual(cfg.stage_policy.enabled_stages, [3, 4])

    def test_credentials_not_in_manifest(self):
        from tests.helpers import make_config

        os.environ["ANTHROPIC_API_KEY"] = "sk-secret-value"
        tmp = make_config_dir()
        cfg = make_config(tmp)
        fingerprint = cfg.core_fingerprint()
        serialized = json.dumps(fingerprint)
        self.assertNotIn("sk-secret-value", serialized)
        util.read_json  # touch import


def make_config_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="cfg-"))


if __name__ == "__main__":
    unittest.main()
