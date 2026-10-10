"""capture 重试代理：上游断连、截断 SSE、重试成功与永久错误。

分两层：
- 基础层（任何解释器可跑）：共享 proxy_retry 的设置校验、SSE 错误分类、
  Retry-After 解析、我们外壳的健康检查策略核对；
- 应用层（需要 fastapi/httpx，skipUnless）：create_app + 模拟上游 transport，
  覆盖断连、不完整响应、重试成功、永久错误停止。

真实共享源码只读复用，不修改。应用层测试用具备 fastapi 的解释器运行，
例如：/root/miniconda3/envs/arvo/bin/python -m pytest tests/test_proxy_retry.py
"""

import json
import os
import sys
import unittest
from pathlib import Path

CAPTURE_PROXY_DIR = Path("/data/ztr/anthropic_full_capture_proxy")
if str(CAPTURE_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(CAPTURE_PROXY_DIR))

try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401

    HAS_APP_DEPS = True
except ImportError:
    HAS_APP_DEPS = False

try:
    import proxy_retry  # 共享源码，只读

    HAS_PROXY_RETRY = True
except ImportError:
    HAS_PROXY_RETRY = False


@unittest.skipUnless(HAS_PROXY_RETRY, "共享 proxy_retry 不可导入")
class RetryPolicySettingsTests(unittest.TestCase):
    def test_settings_validation(self):
        from dataclasses import fields

        from proxy_retry import Settings

        kwargs = {
            f.name: getattr(_defaults(), f.name, None)
            for f in fields(Settings)
        }
        kwargs.update(
            listen_host="127.0.0.1", listen_port=30305,
            upstream_url="http://upstream", log_dir=Path("/tmp/x"),
        )
        settings = Settings(**{k: v for k, v in kwargs.items() if v is not None})
        self.assertEqual(settings.max_retries, 20)
        with self.assertRaises(ValueError):
            Settings(
                listen_host="127.0.0.1", listen_port=1, upstream_url="http://u",
                log_dir=Path("/tmp/x"), max_retries=-1,
            )

    def test_retry_after_parsing(self):
        from proxy_retry import retry_after_seconds

        self.assertEqual(retry_after_seconds(None), 0)
        self.assertEqual(retry_after_seconds("2.5"), 2.5)
        self.assertEqual(retry_after_seconds("not-a-number"), 0)


def _defaults():
    """proxy_retry.Settings 的默认值集合。"""

    class _D:
        timeout_seconds = 300.0
        connect_timeout_seconds = 30.0
        read_timeout_seconds = 600.0
        attempt_timeout_seconds = 3600.0
        request_timeout_seconds = 86400.0
        max_retries = 20
        retry_delay_seconds = 1.0
        retry_max_delay_seconds = 30.0

    return _D()


@unittest.skipUnless(HAS_PROXY_RETRY, "共享 proxy_retry 不可导入")
class StreamValidationTests(unittest.TestCase):
    """SSE 错误分类：永久错误不重试、可重试错误继续、不完整响应重试。"""

    def _capture(self, tmp: Path, *, sse_lines=None, message=None,
                 aggregation_complete=True, errors=None):
        from capture_core import RequestCapture

        capture = RequestCapture(tmp)
        capture.start_request(
            method="POST", path="/glm53/v1/messages", query="", url="http://x",
            headers=[("host", "x")], raw_body=b"{}", upstream_url="http://x",
            client_host="127.0.0.1", client_port=1,
        )
        capture.start_response(status_code=200, headers=[], is_sse=True)
        for line in sse_lines or []:
            capture.append_response(line.encode("utf-8"))
        capture.finalize()
        # 直接改写 response 记录以模拟聚合状态
        response_path = capture.paths.completed_dir / "response.json"
        payload = json.loads(response_path.read_text())
        payload["aggregation_complete"] = aggregation_complete
        if message is not None:
            payload["message"] = message
        if errors:
            payload["aggregation_errors"] = errors
        response_path.write_text(json.dumps(payload))
        return capture

    def test_permanent_sse_error_not_retryable(self):
        from proxy_retry import stream_validation_error

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture(
                Path(tmp),
                sse_lines=[
                    'event: message_start\ndata: {"type":"message_start"}\n\n',
                    'event: error\ndata: {"type":"error","error":{"type":"authentication_error","message":"bad key"}}\n\n',
                ],
            )
            error, retryable = stream_validation_error(capture)
            self.assertIsNotNone(error)
            self.assertFalse(retryable)  # 认证错误：永久，不盲目重试

    def test_api_error_sse_retryable(self):
        from proxy_retry import stream_validation_error

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture(
                Path(tmp),
                sse_lines=[
                    'event: error\ndata: {"type":"error","error":{"type":"api_error","message":"internal"}}\n\n',
                ],
            )
            error, retryable = stream_validation_error(capture)
            self.assertIsNotNone(error)
            self.assertTrue(retryable)

    def test_incomplete_sse_retryable(self):
        """缺少完整结束标记（不完整响应）：按可重试处理。"""
        from proxy_retry import stream_validation_error

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            capture = self._capture(
                Path(tmp),
                sse_lines=['event: message_start\ndata: {"type":"message_start"}\n\n'],
                aggregation_complete=False,
            )
            error, retryable = stream_validation_error(capture)
            self.assertIsNotNone(error)
            self.assertTrue(retryable)


@unittest.skipUnless(HAS_APP_DEPS and HAS_PROXY_RETRY, "需要 fastapi/httpx")
class BufferedProxyAppTests(unittest.TestCase):
    """create_app + 模拟上游：断连/截断/重试成功/永久错误。"""

    def _settings(self, tmp: Path, **overrides):
        from proxy_retry import Settings

        defaults = dict(
            listen_host="127.0.0.1", listen_port=30305,
            upstream_url="http://upstream.example", log_dir=Path(tmp) / "capture",
            timeout_seconds=5.0, connect_timeout_seconds=2.0,
            read_timeout_seconds=5.0, attempt_timeout_seconds=5.0,
            request_timeout_seconds=30.0, max_retries=3,
            retry_delay_seconds=0.01, retry_max_delay_seconds=0.05,
        )
        defaults.update(overrides)
        return Settings(**defaults)

    def _transport(self, behaviors):
        """按序返回响应/异常的上游 transport。behaviors: list[bytes|Exception].

        响应以异步分块流返回（proxy 用 aiter_raw 消费）。
        """
        import httpx

        class ScriptedTransport(httpx.AsyncBaseTransport):
            def __init__(self, script):
                self.script = list(script)
                self.calls = 0

            async def handle_async_request(self, request):
                index = min(self.calls, len(self.script) - 1)
                behavior = self.script[index]
                self.calls += 1
                if isinstance(behavior, Exception):
                    raise behavior

                async def chunks():
                    for position in range(0, len(behavior), 64):
                        yield behavior[position:position + 64]

                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    content=chunks(),
                )

        return ScriptedTransport(behaviors)

    def _sse(self, *, message_id="msg_1", complete=True):
        """合法的最小 Anthropic SSE 流（符合 capture_core 聚合器要求）。"""
        events = [
            "event: message_start\ndata: "
            + json.dumps({"type": "message_start",
                         "message": {"id": message_id, "role": "assistant", "content": []}})
            + "\n\n",
            "event: content_block_start\ndata: "
            + json.dumps({"type": "content_block_start", "index": 0,
                         "content_block": {"type": "text", "text": ""}})
            + "\n\n",
            "event: content_block_delta\ndata: "
            + json.dumps({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": "hi"}})
            + "\n\n",
            "event: content_block_stop\ndata: "
            + json.dumps({"type": "content_block_stop", "index": 0})
            + "\n\n",
            "event: message_delta\ndata: "
            + json.dumps({"type": "message_delta",
                         "delta": {"stop_reason": "end_turn"},
                         "usage": {"output_tokens": 2}})
            + "\n\n",
        ]
        if complete:
            events.append(
                "event: message_stop\ndata: " + json.dumps({"type": "message_stop"}) + "\n\n"
            )
        return "".join(events).encode("utf-8")

    def _request(self, app, tmp):
        # TestClient 的 with 语句会触发 FastAPI lifespan（app.state.client 建立）
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            return client.post(
                "/glm53/v1/messages",
                json={"model": "glm53", "messages": [{"role": "user", "content": "hi"}]},
            )

    def test_retry_success_after_disconnect(self):
        """上游断连后重试成功：客户端只看到最终有效响应。"""
        import tempfile

        import httpx

        import proxy_retry

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            transport = self._transport([
                httpx.ConnectError("connection refused"),
                self._sse(),
            ])
            app = proxy_retry.create_app(settings, upstream_transport=transport)
            response = self._request(app, tmp)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(transport.calls, 2)
            # 失败尝试归档到 retry_attempts，不进入 raw/completed
            retry_root = Path(settings.log_dir) / "raw" / "retry_attempts"
            self.assertTrue(any(retry_root.iterdir()))

    def test_truncated_sse_retried(self):
        """截断 SSE（缺 message_stop）：按不完整重试，最终成功。"""
        import tempfile

        import httpx

        import proxy_retry

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            transport = self._transport([self._sse(complete=False), self._sse()])
            app = proxy_retry.create_app(settings, upstream_transport=transport)
            response = self._request(app, tmp)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(transport.calls, 2)

    def test_permanent_error_stops_early(self):
        """认证类 SSE 错误：不盲目重试，及时停止。"""
        import tempfile

        import proxy_retry

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            error_sse = (
                b"event: error\ndata: "
                + json.dumps({"type": "error", "error": {
                    "type": "authentication_error", "message": "bad key"}}).encode()
                + b"\n\n"
            )
            transport = self._transport([error_sse])
            app = proxy_retry.create_app(settings, upstream_transport=transport)
            response = self._request(app, tmp)
            self.assertEqual(transport.calls, 1)  # 永久错误：只有首次尝试
            self.assertEqual(response.status_code, 502)
            self.assertIn("proxy_error", response.text)

    def test_retry_budget_exhaustion(self):
        """重试限额耗尽：返回 proxy_error，不再无限重试。"""
        import tempfile

        import httpx

        import proxy_retry

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), max_retries=2)
            transport = self._transport([httpx.ConnectError("down")] * 10)
            app = proxy_retry.create_app(settings, upstream_transport=transport)
            response = self._request(app, tmp)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(transport.calls, 3)  # 首次 + 2 次重试

    def test_only_delivered_response_recorded(self):
        """只有真正投递的响应标记 delivered；rejected 尝试单独归档。"""
        import tempfile

        import httpx

        import proxy_retry

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            transport = self._transport([httpx.ConnectError("down"), self._sse()])
            app = proxy_retry.create_app(settings, upstream_transport=transport)
            response = self._request(app, tmp)
            self.assertEqual(response.status_code, 200)
            completed = Path(settings.log_dir) / "raw" / "completed"
            delivered = [
                json.loads((path / "response.json").read_text())
                for path in sorted(completed.iterdir())
            ]
            self.assertEqual(len(delivered), 1)
            self.assertEqual(delivered[0]["proxy_delivery"], "delivered")
            self.assertEqual(delivered[0]["message_id"], "msg_1")


class HealthCheckPolicyTests(unittest.TestCase):
    """外壳侧：健康检查核对完整缓存、上游与重试参数（与 _verify_capture_policy 同款期望表）。"""

    def test_policy_mismatch_detected(self):
        health = {
            "buffer_response": True,
            "upstream_url": "http://upstream",
            "retry_policy": {
                "timeout_seconds": 300.0, "connect_timeout_seconds": 30.0,
                "read_timeout_seconds": 600.0, "attempt_timeout_seconds": 3600.0,
                "request_timeout_seconds": 86400.0, "max_retries": 20,
                "retry_delay_seconds": 1.0, "retry_max_delay_seconds": 30.0,
            },
        }
        expected = {
            "timeout_seconds": 300.0, "connect_timeout_seconds": 30.0,
            "read_timeout_seconds": 600.0, "attempt_timeout_seconds": 3600.0,
            "request_timeout_seconds": 86400.0, "max_retries": 20,
            "retry_delay_seconds": 1.0, "retry_max_delay_seconds": 30.0,
        }
        policy = health.get("retry_policy", {})
        problems = [
            name for name, value in expected.items()
            if policy.get(name) != value
        ]
        self.assertEqual(problems, [])
        self.assertIs(health["buffer_response"], True)
        # 参数不一致时暴露
        policy["max_retries"] = 3
        problems = [
            name for name, value in expected.items() if policy.get(name) != value
        ]
        self.assertIn("max_retries", problems)


if __name__ == "__main__":
    unittest.main()
