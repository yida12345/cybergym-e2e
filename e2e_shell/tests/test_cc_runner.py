"""CC runner 细节：终态后 idle guard 的触发边界与 effort 证据核对。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from shelllib.cc_runner import ResultIdleGuard, check_effort_evidence


class ResultIdleGuardTests(unittest.TestCase):
    def test_no_result_event_never_fires(self):
        """没有终态 result 事件时：长时间无输出也不触发（正常等待模型响应）。"""
        guard = ResultIdleGuard(idle_seconds=0.2, check_interval=0.05)
        guard.start()
        try:
            guard.feed('{"type":"assistant","message":{"id":"m1"}}\n')
            time.sleep(0.5)
            self.assertIsNone(guard.reason)
        finally:
            guard.stop()

    def test_fires_only_after_result_event(self):
        guard = ResultIdleGuard(idle_seconds=0.2, check_interval=0.05)
        guard.start()
        try:
            guard.feed('{"type":"assistant","message":{"id":"m1"}}\n')
            time.sleep(0.3)
            self.assertIsNone(guard.reason)
            guard.feed('{"type":"result","subtype":"success"}\n')
            deadline = time.monotonic() + 2
            while guard.reason is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(guard.reason)
            self.assertIn("result_idle_timeout", guard.reason)
        finally:
            guard.stop()

    def test_output_resets_idle_window(self):
        guard = ResultIdleGuard(idle_seconds=0.4, check_interval=0.05)
        guard.start()
        try:
            guard.feed('{"type":"result"}\n')
            for _ in range(6):
                time.sleep(0.15)
                guard.feed('{"type":"stream"}\n')  # 仍有输出：重置窗口
            self.assertIsNone(guard.reason)
        finally:
            guard.stop()


class CheckEffortEvidenceTests(unittest.TestCase):
    def _capture(self, root: Path, capture_id: str, body: dict) -> None:
        completed = root / "raw" / "completed" / capture_id
        completed.mkdir(parents=True, exist_ok=True)
        (completed / "request.json").write_text(
            json.dumps({"capture_id": capture_id, "body": body}), encoding="utf-8"
        )

    def test_detects_thinking_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._capture(root, "cap1", {"model": "glm53", "thinking": {"type": "enabled", "budget_tokens": 32768}})
            evidence = check_effort_evidence(root / "raw", "glm53")
            self.assertTrue(evidence["supported"])
            self.assertIn("thinking", evidence["effort_fields"][0])

    def test_no_evidence_reported_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._capture(root, "cap1", {"model": "glm53", "messages": []})
            evidence = check_effort_evidence(root / "raw", "glm53")
            self.assertFalse(evidence["supported"])

    def test_other_model_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._capture(root, "cap1", {"model": "claude-x", "thinking": {"budget_tokens": 1}})
            evidence = check_effort_evidence(root / "raw", "glm53")
            self.assertFalse(evidence["supported"])


if __name__ == "__main__":
    unittest.main()
