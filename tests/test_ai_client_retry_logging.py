# coding=utf-8
"""AIClient attempt 失败分类日志与既有重试行为的离线契约测试。

运行时设置 LITELLM_LOCAL_MODEL_COST_MAP=True，避免 LiteLLM 导入时下载模型表。
completion、sleep 和网络连接均被替换；所有凭据与 URL 均为合成测试数据。
"""

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import call, patch

with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True), \
        patch("socket.create_connection", side_effect=AssertionError("network during import")), \
        patch("socket.socket.connect", side_effect=AssertionError("network during import")), \
        patch("socket.socket.connect_ex", side_effect=AssertionError("network during import")):
    from litellm.exceptions import ServiceUnavailableError, Timeout
    from trendradar.ai.client import AIClient, RETRY_BACKOFF_SECONDS, TIMEOUT_RETRY_LIMIT


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
        env = patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
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
        # 抖动固定为0，使 sleep 断言拿到确定值；另有专测验证抖动区间。
        self._patch("trendradar.ai.client.random.uniform", return_value=0.0)
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
        models = config.get("FALLBACK_MODELS", [])
        if models:
            config["FALLBACK_API_BASE"] = "@".join([FAKE_API_BASE] * len(models))
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

    def _assert_calls(self, models, timeout=120):
        self.assertEqual(
            self.completion.call_args_list,
            [
                call(
                    model=model, messages=self.messages, timeout=timeout,
                    api_key=FAKE_API_KEY, api_base=FAKE_API_BASE, max_retries=0, num_retries=0,
                )
                for model in models
            ],
        )

    def test_retry_constants_match_shared_contract(self):
        self.assertEqual(RETRY_BACKOFF_SECONDS, (5, 8))
        self.assertEqual(TIMEOUT_RETRY_LIMIT, 1)

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
            [f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=1, timeout=120s"],
        )

    def test_503_retries_log_once_per_failure_then_succeed(self):
        client = self._client()
        self.completion.side_effect = [self._unavailable(), self._unavailable(), self.response]

        self.assertEqual(client.chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL] * 3)
        self.assertEqual(self.sleep.call_args_list, [call(5), call(8)])
        self.assertEqual(
            self.output.getvalue().splitlines(),
            [
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=1, timeout=120s",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=2, timeout=120s",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt=3, timeout=120s",
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

    def test_fallback_runs_after_primary_backoff_exhaustion(self):
        client = self._client(FALLBACK_MODELS=[FALLBACK_MODEL], TIMEOUT=37)
        self.completion.side_effect = [
            self._unavailable(), self._unavailable(), self._unavailable(),
            self._unavailable(FALLBACK_MODEL), self._unavailable(FALLBACK_MODEL), self.response,
        ]

        self.assertEqual(client.chat(self.messages), "offline answer")

        # 5xx/429 拥塞类走 5/8 两档；默认预算耗尽后切备用；每个模型重新取得两档。
        self._assert_calls([PRIMARY_MODEL] * 3 + [FALLBACK_MODEL] * 3, timeout=37)
        self.assertEqual(self.sleep.call_args_list, [call(5), call(8), call(5), call(8)])
        self.assertEqual(
            self._failure_lines(), ["[AI] attempt 失败: ServiceUnavailableError(503)"] * 5
        )
        self.assertEqual(
            self.output.getvalue().splitlines()[6:],
            [
                f"[AI] 模型 {PRIMARY_MODEL} 调用失败，尝试备用模型",
                f"[AI] 请求开始: model={FALLBACK_MODEL}, attempt=1, timeout=37s, candidate=fallback:1",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                f"[AI] 请求开始: model={FALLBACK_MODEL}, attempt=2, timeout=37s, candidate=fallback:1",
                "[AI] attempt 失败: ServiceUnavailableError(503)",
                f"[AI] 请求开始: model={FALLBACK_MODEL}, attempt=3, timeout=37s, candidate=fallback:1",
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
        # 每个模型：首击503走5秒档，第二次非5xx异常且已到重试预算→切换/抛出。
        self.assertEqual(self.sleep.call_args_list, [call(5), call(5)])
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

    def test_5xx_backoff_uses_tiers_then_generic_cap(self):
        self.completion.side_effect = [self._unavailable() for _ in range(6)] + [self.response]

        self.assertEqual(self._client(NUM_RETRIES=6).chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL] * 7)
        # 先消耗 5/8 拥塞档；显式放大预算时，既有通用指数算法继续从4秒升至封顶8秒。
        self.assertEqual(
            self.sleep.call_args_list,
            [call(5), call(8), call(4), call(8), call(8), call(8)],
        )
        self.assertEqual(
            self._failure_lines(), ["[AI] attempt 失败: ServiceUnavailableError(503)"] * 6
        )

    def test_call_overrides_retry_count_and_timeout_without_forwarding_retry_option(self):
        client = self._client(NUM_RETRIES=6, TIMEOUT=91)
        final_error = RuntimeError(PRIVATE_MESSAGE)
        self.completion.side_effect = [RuntimeError(PRIVATE_MESSAGE), final_error]

        with self.assertRaises(RuntimeError) as raised:
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
                f"[AI] 请求开始: model={PRIMARY_MODEL}, attempt={attempt}, timeout=12.5s"
                for attempt in (1, 2)
            ],
        )
        self.assertEqual(client.timeout, 91)
        self.assertEqual(client.num_retries, 6)

    def test_timeout_switches_immediately_without_retry_or_sleep(self):
        # 408 确定性慢：单模型只尝试一次，立即切换备用模型。
        client = self._client(FALLBACK_MODELS=[FALLBACK_MODEL])
        timeout_error = Timeout(message=PRIVATE_MESSAGE, model=PRIMARY_MODEL, llm_provider="openai")
        self.completion.side_effect = [timeout_error, self.response]

        self.assertEqual(client.chat(self.messages), "offline answer")

        self._assert_calls([PRIMARY_MODEL, FALLBACK_MODEL])
        self.sleep.assert_not_called()
        self.assertEqual(
            self._failure_lines(),
            ["[AI] attempt 失败: Timeout(408)"],
        )

    def test_http_408_and_named_timeout_each_switch_with_large_retry_budget(self):
        named_timeout = type("Timeout", (Exception,), {})(PRIVATE_MESSAGE)
        http_timeout = RuntimeError(PRIVATE_MESSAGE)
        http_timeout.status_code = 408
        for error in (http_timeout, named_timeout):
            with self.subTest(error_type=type(error).__name__):
                self.completion.reset_mock()
                self.sleep.reset_mock()
                self.output.seek(0)
                self.output.truncate(0)
                self.completion.side_effect = [error, self.response]
                client = self._client(NUM_RETRIES=20, FALLBACK_MODELS=[FALLBACK_MODEL])

                self.assertEqual(client.chat(self.messages), "offline answer")

                self._assert_calls([PRIMARY_MODEL, FALLBACK_MODEL])
                self.sleep.assert_not_called()
                self.assertEqual(client.last_model, FALLBACK_MODEL)
                self.assertEqual(len(self._failure_lines()), 1)

    def test_backoff_jitter_adds_at_most_one_second(self):
        self.completion.side_effect = [self._unavailable(), self.response]
        with patch("trendradar.ai.client.random.uniform", return_value=0.5) as uniform, \
                patch("trendradar.ai.client.time.sleep") as real_sleep:
            self._client().chat(self.messages)
        uniform.assert_called_once_with(0, 1)
        real_sleep.assert_called_once_with(5.5)

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

    def test_litellm_timeout_reraises_without_retry_with_unchanged_timeout(self):
        original = Timeout(message=PRIVATE_MESSAGE, model=PRIMARY_MODEL, llm_provider="openai")
        self.completion.side_effect = original
        client = self._client(TIMEOUT=45)

        with self.assertRaises(Timeout) as raised:
            client.chat(self.messages)

        self.assertIs(raised.exception, original)
        self._assert_calls([PRIMARY_MODEL], timeout=45)
        self.sleep.assert_not_called()
        self.assertIsNone(client.last_model)
        self.assertIsNone(client.last_finish_reason)
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
