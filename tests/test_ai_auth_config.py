# coding=utf-8
"""Offline contracts for one positional configuration and explicit SDK policy."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
    from trendradar.ai.client import AIClient, DEFAULT_TIMEOUT, build_keyword_client
    from trendradar.ai_config import DEFAULT_API_BASES
    from trendradar.core import loader

PRIMARY_MODEL = "gemini/synthetic-primary"
GEMINI_FALLBACK = "gemini/synthetic-lite"
OPENAI_FALLBACK = "openai/synthetic-actual-model"
PRIMARY_KEY = "synthetic-primary-key-do-not-log"
PRIMARY_BASE = "https://gemini.example.invalid/v1beta"
OPENAI_KEY = "synthetic-openai-key-do-not-log"
OPENAI_BASE = "https://openai-relay.example.invalid/v1"
PRIVATE_ERROR = f"private synthetic upstream body {PRIMARY_KEY} {OPENAI_KEY} {PRIMARY_BASE} {OPENAI_BASE}"


class SyntheticHTTPError(Exception):
    def __init__(self, status_code):
        super().__init__(PRIVATE_ERROR)
        self.status_code = status_code


class OfflineAITestCase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.messages = [{"role": "user", "content": "synthetic offline prompt"}]
        self.response = {"choices": [{"message": {"content": "offline answer"}, "finish_reason": "stop"}]}
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.completion = self._patch("trendradar.ai.client.completion", return_value=self.response)
        self.sleep = self._patch("trendradar.ai.client.time.sleep")
        self._patch("trendradar.ai.client.random.uniform", return_value=0.0)
        self.network_guards = [self._patch(target, side_effect=AssertionError("offline test attempted network"))
                              for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo")]

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _config(self, **overrides):
        value = {"MODEL": PRIMARY_MODEL, "API_KEY": PRIMARY_KEY, "API_BASE": PRIMARY_BASE,
                 "FALLBACK_MODELS": [GEMINI_FALLBACK, OPENAI_FALLBACK],
                 "FALLBACK_API_BASE": PRIMARY_BASE + "@" + OPENAI_BASE,
                 "FALLBACK_API_KEY": "@" + OPENAI_KEY}
        value.update(overrides)
        return value

    def _client(self, **overrides):
        return AIClient(self._config(**overrides))

    def _request(self, model, key=PRIMARY_KEY, base=PRIMARY_BASE, timeout=120):
        return call(model=model, messages=self.messages, timeout=timeout, api_key=key,
                    api_base=base or DEFAULT_API_BASES[model.partition('/')[0]], max_retries=0, num_retries=0)

    def _assert_private_values_absent(self):
        for value in (PRIMARY_KEY, PRIMARY_BASE, OPENAI_KEY, OPENAI_BASE, PRIVATE_ERROR):
            self.assertNotIn(value, self.output.getvalue())


class AIAuthConfigTests(OfflineAITestCase):
    def test_environment_overrides_yaml_and_preserves_positions(self):
        with patch.dict(os.environ, {"AI_MODEL": PRIMARY_MODEL, "AI_API_KEY": PRIMARY_KEY,
                                    "AI_API_BASE": PRIMARY_BASE, "AI_FALLBACK_MODELS": GEMINI_FALLBACK + ',' + OPENAI_FALLBACK,
                                    "AI_FALLBACK_API_BASE": PRIMARY_BASE + '@' + OPENAI_BASE,
                                    "AI_FALLBACK_API_KEY": '@' + OPENAI_KEY, "AI_TIMEOUT": "37"}):
            config = loader._load_ai_config({"ai": {"model": "openai/yaml", "timeout": 4}})
        self.assertEqual(config, dict(self._config(TIMEOUT=37), EXTRA_PARAMS={}))

    def test_blank_env_uses_yaml_model_order_without_deduplication(self):
        ai = {"model": PRIMARY_MODEL, "api_key": PRIMARY_KEY, "fallback_models": [GEMINI_FALLBACK, GEMINI_FALLBACK]}
        with patch.dict(os.environ, {"AI_FALLBACK_MODELS": "   ", "AI_FALLBACK_API_BASE": "", "AI_FALLBACK_API_KEY": ""}):
            client = AIClient(loader._load_ai_config({"ai": ai}))
        self.assertEqual([c.id for c in client.candidates], ['main', 'fallback:1', 'fallback:2'])

    def test_retired_environment_and_key_file_are_not_used(self):
        with patch.dict(os.environ, {"AI_OPENAI_API_KEY": OPENAI_KEY, "AI_OPENAI_API_BASE": OPENAI_BASE,
                                    "AI_OPENAI_API_KEY_FILE": "/synthetic/must-not-open"}), patch.object(loader, '_read_secret_file') as read:
            config = loader._load_ai_config({})
        self.assertFalse(any('OPENAI' in name for name in config))
        read.assert_not_called()

    def test_client_does_not_reread_ambient_configuration(self):
        with patch.dict(os.environ, {"AI_API_KEY": OPENAI_KEY, "AI_FALLBACK_API_KEY": OPENAI_KEY,
                                    "AI_FALLBACK_API_BASE": OPENAI_BASE}):
            client = AIClient({"MODEL": PRIMARY_MODEL})
        self.assertFalse(client.validate_config()[0])
        self.assertEqual(client.api_key, '')
        self.assertFalse(client.configuration_error)

    def test_main_key_file_still_uses_private_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'key'
            path.write_text(PRIMARY_KEY)
            path.chmod(0o600)
            with patch.dict(os.environ, {"AI_API_KEY_FILE": str(path)}):
                self.assertEqual(loader._load_ai_config({})['API_KEY'], PRIMARY_KEY)
            path.chmod(0o644)
            with patch.dict(os.environ, {"AI_API_KEY_FILE": str(path)}), self.assertRaises(ValueError):
                loader._load_ai_config({})

    def test_daily_weekly_share_config_and_default_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.yaml'
            path.write_text('ai:\n  model: gemini/synthetic-primary\n')
            with patch.dict(os.environ, {"AI_API_KEY": PRIMARY_KEY}), patch.object(loader, '_load_timeline_data', return_value={}):
                daily, weekly = loader.load_config(str(path))['AI'], loader.load_ai_config(str(path))
            self.assertEqual(daily, weekly)
            self.assertEqual(AIClient(daily).timeout, DEFAULT_TIMEOUT)
            self.assertEqual(DEFAULT_TIMEOUT, 120)

    def test_invalid_model_syntax_rejected_even_when_auth_columns_empty(self):
        for raw in ('gemini/a,,gemini/b', 'gemini/a gemini/b', 'missing-provider'):
            with self.subTest(raw=raw), patch.dict(os.environ, {"AI_FALLBACK_MODELS": raw}), self.assertRaises(ValueError):
                loader._load_ai_config({})

    def test_invalid_timeout_is_not_silently_ignored(self):
        for raw in ('0', '-1', '1.5', 'private-invalid-timeout'):
            with self.subTest(raw=raw), patch.dict(os.environ, {"AI_TIMEOUT": raw}), self.assertRaisesRegex(ValueError, 'AI_TIMEOUT'):
                loader._load_ai_config({})


class AIAuthRoutingTests(OfflineAITestCase):
    def test_primary_success_and_explicit_sdk_retry_policy(self):
        client = self._client()
        self.assertEqual(client.chat(self.messages), 'offline answer')
        self.assertEqual(self.completion.call_args_list, [self._request(PRIMARY_MODEL)])
        self.assertEqual(client.last_candidate_id, 'main')
        self._assert_private_values_absent()

    def test_three_level_timeout_chain_preserves_auth_and_slots(self):
        self.completion.side_effect = [SyntheticHTTPError(408), SyntheticHTTPError(408), self.response]
        client = self._client()
        self.assertEqual(client.chat(self.messages), 'offline answer')
        self.assertEqual(self.completion.call_args_list, [self._request(PRIMARY_MODEL), self._request(GEMINI_FALLBACK), self._request(OPENAI_FALLBACK, OPENAI_KEY, OPENAI_BASE)])
        self.assertEqual(client.last_candidate_id, 'fallback:2')
        self.sleep.assert_not_called()
        self._assert_private_values_absent()

    def test_exhausted_chain_preserves_last_exception(self):
        error = SyntheticHTTPError(408)
        self.completion.side_effect = error
        client = self._client()
        with self.assertRaises(SyntheticHTTPError) as raised:
            client.chat(self.messages)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.completion.call_count, 3)
        self.assertIsNone(client.last_candidate_id)

    def test_empty_backup_url_never_inherits_custom_primary(self):
        client = self._client(FALLBACK_API_BASE='@' + OPENAI_BASE)
        self.assertEqual(client.candidates[1].error_kind, 'api_key')
        self.assertTrue(client.configuration_warnings())

    def test_adding_a_slot_does_not_change_the_previous_empty_slot(self):
        first = AIClient({"MODEL": PRIMARY_MODEL, "API_KEY": PRIMARY_KEY, "API_BASE": PRIMARY_BASE,
                          "FALLBACK_MODELS": [GEMINI_FALLBACK]})
        second = self._client(FALLBACK_API_BASE='@' + OPENAI_BASE)
        self.assertEqual(first.candidates[1], second.candidates[1])

    def test_repeated_primary_is_a_real_first_backup_and_never_crashes(self):
        client = self._client(FALLBACK_MODELS=[PRIMARY_MODEL], FALLBACK_API_BASE=PRIMARY_BASE, FALLBACK_API_KEY='')
        self.assertEqual(len(client.candidates), 2)
        self.assertEqual(build_keyword_client(client).candidates[0].id, 'fallback:1')

    def test_controlled_extra_params_fail_without_calling_sdk(self):
        for name in ('api_key', 'api_base', 'base_url', 'client', 'fallbacks', 'max_retries', 'num_retries', 'timeout', 'messages'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '受控字段'):
                self._client(EXTRA_PARAMS={name: 'private-test-value'}).chat(self.messages)
        self.completion.assert_not_called()

    def test_explicit_call_timeout_is_not_lost(self):
        self._client().chat(self.messages, timeout=37, num_retries=0)
        self.assertEqual(self.completion.call_args.kwargs['timeout'], 37)
        self.assertEqual(self.completion.call_args.kwargs['max_retries'], 0)


class KeywordClientAuthTests(OfflineAITestCase):
    def test_keyword_first_slot_has_complete_own_identity(self):
        parent = self._client(FALLBACK_MODELS=[OPENAI_FALLBACK], FALLBACK_API_BASE=OPENAI_BASE, FALLBACK_API_KEY=OPENAI_KEY)
        child = build_keyword_client(build_keyword_client(parent))
        child.chat(self.messages)
        self.assertEqual((child.model, child.api_key, child.api_base), (OPENAI_FALLBACK, OPENAI_KEY, OPENAI_BASE))
        self.assertEqual(child.last_candidate_id, 'fallback:1')
        self.assertIsNone(parent.last_model)
        self.assertEqual(len(parent.candidates), 2)

    def test_invalid_first_slot_does_not_promote_second_or_main(self):
        child = build_keyword_client(self._client(FALLBACK_API_BASE='@' + OPENAI_BASE))
        with self.assertRaisesRegex(ValueError, '第 1 项'):
            child.chat(self.messages)
        self.completion.assert_not_called()

    def test_selected_independent_slot_does_not_need_main_key(self):
        child = build_keyword_client(self._config(API_KEY='', FALLBACK_MODELS=[OPENAI_FALLBACK], FALLBACK_API_BASE=OPENAI_BASE, FALLBACK_API_KEY=OPENAI_KEY))
        self.assertEqual(child.chat(self.messages), 'offline answer')

    def test_no_backup_uses_main_without_changing_parent(self):
        parent = self._client(FALLBACK_MODELS=[], FALLBACK_API_BASE='', FALLBACK_API_KEY='')
        child = build_keyword_client(parent)
        self.assertEqual(child.chat(self.messages), 'offline answer')
        self.assertEqual(child.last_candidate_id, 'main')
        self.assertIsNone(parent.last_model)


if __name__ == '__main__':
    unittest.main()
