# coding=utf-8
"""AIClient attempt 失败分类日志与既有重试行为的离线契约测试。

运行时设置 LITELLM_LOCAL_MODEL_COST_MAP=True，避免 LiteLLM 导入时下载模型表。
completion、sleep 和网络连接均被替换；所有凭据与 URL 均为合成测试数据。
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import call, patch

from litellm.exceptions import ServiceUnavailableError, Timeout

from trendradar.ai.client import AIClient


PRIMARY_MODEL = "openai/test-primary"
FALLBACK_MODEL = "openai/test-fallback"
FAKE_API_KEY = "sk-synthetic-offline-test-key-not-a-real-secret"
FAKE_API_BASE = "https://relay.example.invalid/v1"
PRIVATE_MESSAGE = (
    "synthetic-private-error-body: "
    f"POST {FAKE_API_BASE}/chat/completions?api_key={FAKE_API_KEY} "
    f"Authorization: Bearer {FAKE_API_KEY}\nprivate-upstream-response"
)
FAILURE_PREFIX = "[AI] attempt 失败: "


class UnprintableError(Exception):
    """异常分类日志不得求值异常正文或 repr。"""

    def __str__(self):
        raise AssertionError("exception body must not be formatted")

    def __repr__(self):
        raise AssertionError("exception repr must not be formatted")


class AIClientRetryLoggingTests(unittest.TestCase):
    def setUp(self):
        self.messages = [{"role": "user", "content": "offline test prompt"}]
        self.response = {
            "choices": [{"message": {"content": "offline answer"}, "finish_reason": "stop"}]
        }
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.completion = self._patch(
            "trendradar.ai.client.completion", return_value=self.response
        )
        self.sleep = self._patch("trendradar.ai.client.time.sleep")
        self.network_guards = [
            self._patch(
                target, side_effect=AssertionError("offline test attempted network access")
            )
            for target in (
                "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"
            )
        ]

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _client(self, **overrides):
        config = {
            "MODEL": PRIMARY_MODEL,
            "API_KEY": FAKE_API_KEY,
            "API_BASE": FAKE_API_BASE,
        }
        config.update(overrides)
        return AIClient(config)

    def _unavailable(self, model=PRIMARY_MODEL):
        return ServiceUnavailableError(
            message=PRIVATE_MESSAGE, llm_provider="openai", model=model
        )

    def _failure_lines(self):
        return [
            line for line in self.output.getvalue().splitlines()
            if line.startswith(FAILURE_PREFIX)
        ]

    def _assert_calls(self, models, timeout=240):
        self.assertEqual(
            self.completion.call_args_list,
            [
                call(
                    model=model, messages=self.messages, timeout=timeout,
                    api_key=FAKE_API_KEY, api_base=FAKE_API_BASE,
                )
                for model in models
            ],
        )

    def test_first_attempt_success_has_no_failure_log_or_sleep(self):
        client = self._client()

        self.assertEqual(client.chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL])
        self.sleep.assert_not_called()
        self.assertEqual(self._failure_lines(), [])
        self.assertEqual(client.last_model, PRIMARY_MODEL)
        self.assertEqual(client.last_finish_reason, "stop")
        self.assertEqual(
            self.output.getvalue().splitlines(),
            [f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=1/3, timeout=240s"],
        )

    def test_503_retries_log_once_per_failure_then_succeed(self):
        client = self._client()
        self.completion.side_effect = [self._unavailable(), self._unavailable(), self.response]

        self.assertEqual(client.chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL] * 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertEqual(
            self.output.getvalue().splitlines(),
            [
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=1/3, timeout=240s",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=2/3, timeout=240s",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=3/3, timeout=240s",
            ],
        )
        self.assertEqual(client.last_model, PRIMARY_MODEL)
        self.assertEqual(client.last_finish_reason, "stop")

    def test_exception_without_status_logs_class_name_only(self):
        self.completion.side_effect = [RuntimeError(PRIVATE_MESSAGE), self.response]

        self.assertEqual(self._client(NUM_RETRIES=1).chat(self.messages), "offline answer")

        self.assertEqual(self._failure_lines(), ["[AI] attempt 失败: RuntimeError"])
        self._assert_calls([PRIMARY_MODEL] * 2)
        self.sleep.assert_called_once_with(1)

    def test_fallback_runs_after_primary_exhaustion_and_resets_backoff(self):
        client = self._client(FALLBACK_MODELS=[FALLBACK_MODEL], TIMEOUT=37)
        self.completion.side_effect = [
            self._unavailable(), self._unavailable(), self._unavailable(),
            self._unavailable(FALLBACK_MODEL), self.response,
        ]

        self.assertEqual(client.chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL] * 3 + [FALLBACK_MODEL] * 2, timeout=37)
        # 主模型最后一次失败与 fallback 切换之间不额外等待；备用重试从 1 秒开始。
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2), call(1)])
        self.assertEqual(
            self._failure_lines(), ["[AI] attempt 失败: ServiceUnavailableError(503)"] * 4
        )
        self.assertEqual(
            self.output.getvalue().splitlines()[6:],
            [
                f"[AI] 模型 {PRIMARY_MODEL} 调用失败，尝试备用模型",
                f"[AI] 请求开始: model={FALLBACK_MODEL}, attempt=1/3, timeout=37s",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                f"[AI] 请求开始: model={FALLBACK_MODEL}, attempt=2/3, timeout=37s",
            ],
        )
        self.assertEqual(client.last_model, FALLBACK_MODEL)
        self.assertEqual(client.last_finish_reason, "stop")

    def test_exhausted_chain_reraises_original_final_exception(self):
        client = self._client(NUM_RETRIES=1, FALLBACK_MODELS=[FALLBACK_MODEL])
        client.last_model = "openai/previous-success"
        client.last_finish_reason = "length"
        final_error = ValueError(PRIVATE_MESSAGE)
        self.completion.side_effect = [
            self._unavailable(), RuntimeError(PRIVATE_MESSAGE),
            self._unavailable(FALLBACK_MODEL), final_error,
        ]

        with self.assertRaises(ValueError) as raised:
            client.chat(self.messages)

        self.assertIs(raised.exception, final_error)
        self._assert_calls([PRIMARY_MODEL] * 2 + [FALLBACK_MODEL] * 2)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(1)])
        self.assertEqual(
            self._failure_lines(),
            [
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                "[AI] attempt 失败: RuntimeError",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                "[AI] attempt 失败: ValueError",
            ],
        )
        self.assertIsNone(client.last_model)
        self.assertIsNone(client.last_finish_reason)

    def test_retry_backoff_remains_exponential_and_capped_at_eight_seconds(self):
        self.completion.side_effect = [self._unavailable() for _ in range(6)] + [self.response]

        self.assertEqual(self._client(NUM_RETRIES=6).chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL] * 7)
        self.assertEqual(
            self.sleep.call_args_list, [call(1), call(2), call(4), call(8), call(8), call(8)]
        )
        self.assertEqual(
            self._failure_lines(), ["[AI] attempt 失败: ServiceUnavailableError(503)"] * 6
        )

    def test_call_overrides_retry_count_and_timeout_without_forwarding_retry_option(self):
        client = self._client(NUM_RETRIES=6, TIMEOUT=91)
        final_error = self._unavailable()
        self.completion.side_effect = [self._unavailable(), final_error]

        with self.assertRaises(ServiceUnavailableError) as raised:
            client.chat(self.messages, num_retries=1, timeout=12.5)

        self.assertIs(raised.exception, final_error)
        self._assert_calls([PRIMARY_MODEL] * 2, timeout=12.5)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(len(self._failure_lines()), 2)
        request_lines = [
            line for line in self.output.getvalue().splitlines() if "请求开始" in line
        ]
        self.assertEqual(
            request_lines,
            [
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt={attempt}/2, timeout=12.5s"
                for attempt in (1, 2)
            ],
        )
        self.assertEqual(client.timeout, 91)
        self.assertEqual(client.num_retries, 6)

    def test_zero_or_negative_retry_override_still_attempts_once(self):
        for retries in (0, -2):
            with self.subTest(retries=retries):
                self.completion.reset_mock()
                self.sleep.reset_mock()
                self.output.seek(0)
                self.output.truncate(0)
                original = RuntimeError(PRIVATE_MESSAGE)
                self.completion.side_effect = original

                with self.assertRaises(RuntimeError) as raised:
                    self._client(NUM_RETRIES=4).chat(self.messages, num_retries=retries)

                self.assertIs(raised.exception, original)
                self._assert_calls([PRIMARY_MODEL])
                self.sleep.assert_not_called()
                self.assertEqual(self._failure_lines(), ["[AI] attempt 失败: RuntimeError"])

    def test_exception_body_url_and_key_never_appear_in_logs(self):
        self.completion.side_effect = [
            self._unavailable(), RuntimeError(PRIVATE_MESSAGE), self.response
        ]

        self.assertEqual(self._client().chat(self.messages), "offline answer")

        text = self.output.getvalue()
        self.assertEqual(
            self._failure_lines(),
            ["[AI] attempt 失败: ServiceUnavailableError(503)", "[AI] attempt 失败: RuntimeError"],
        )
        for private_value in (
            PRIVATE_MESSAGE, FAKE_API_KEY, FAKE_API_BASE, "relay.example.invalid",
            "synthetic-private-error-body", "Authorization", "private-upstream-response",
        ):
            with self.subTest(private_value=private_value):
                self.assertNotIn(private_value, text)

    def test_logging_does_not_stringify_or_replace_original_exception(self):
        original = UnprintableError(PRIVATE_MESSAGE)
        self.completion.side_effect = original

        with self.assertRaises(UnprintableError) as raised:
            self._client(NUM_RETRIES=0).chat(self.messages)

        self.assertIs(raised.exception, original)
        self.assertEqual(self._failure_lines(), ["[AI] attempt 失败: UnprintableError"])
        self.sleep.assert_not_called()

    def test_litellm_timeout_retries_with_unchanged_timeout_and_status_log(self):
        self.completion.side_effect = [
            Timeout(message=PRIVATE_MESSAGE, model=PRIMARY_MODEL, llm_provider="openai"),
            self.response,
        ]

        self.assertEqual(self._client(TIMEOUT=45).chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL] * 2, timeout=45)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(self._failure_lines(), ["[AI] attempt 失败: Timeout(408)"])

    def test_false_status_values_do_not_add_parentheses(self):
        for status in (None, 0):
            with self.subTest(status=status):
                self.output.seek(0)
                self.output.truncate(0)
                error = RuntimeError(PRIVATE_MESSAGE)
                error.status_code = status
                self.completion.side_effect = [error, self.response]

                self.assertEqual(self._client(NUM_RETRIES=1).chat(self.messages), "offline answer")

                self.assertEqual(self._failure_lines(), ["[AI] attempt 失败: RuntimeError"])


if __name__ == "__main__":
    unittest.main()
