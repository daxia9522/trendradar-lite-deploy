"""Exercise pinned LiteLLM and provider SDKs with in-memory HTTP, not completion mocks."""
import contextlib
import io
import json
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True), patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
    import httpx
    import litellm
    from litellm.llms.openai.openai import OpenAIChatCompletion
    from trendradar.ai.client import AIClient, build_keyword_client, _model_request_params


class AISDKBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True))
        self.stack.enter_context(patch.object(litellm, 'headers', None))
        self.stack.enter_context(patch.object(litellm, 'proxy_auth', None))
        self.stack.enter_context(patch.object(litellm, 'num_retries', None))
        self.stack.enter_context(patch('time.sleep'))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.guards = [self.stack.enter_context(patch(name, side_effect=AssertionError('external network forbidden')))
                       for name in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.create_connection', 'socket.getaddrinfo', 'socket.socket.sendto')]
        self.messages = [{'role': 'user', 'content': 'offline SDK probe'}]

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()

    def _response(self):
        return {'id': 'chatcmpl-synthetic', 'object': 'chat.completion', 'created': 0, 'model': 'synthetic',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'offline answer'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}

    def _run_transport(self, config, handler):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http, \
                patch.object(OpenAIChatCompletion, '_get_sync_http_client', return_value=http), \
                patch.object(OpenAIChatCompletion, 'get_cached_openai_client', return_value=None), \
                patch.object(OpenAIChatCompletion, 'set_cached_openai_client'):
            return AIClient(config).chat(self.messages)

    def _retry(self, failure, expected):
        attempts = []
        def handler(request):
            attempts.append((request.url.host, request.headers.get('authorization')))
            if request.url.host == 'primary.example.invalid':
                if failure == 'timeout':
                    raise httpx.ReadTimeout('synthetic timeout', request=request)
                return httpx.Response(failure, json={'error': {'message': 'synthetic error', 'type': 'server_error'}})
            return httpx.Response(200, json=self._response())
        config = {'MODEL': 'openai/synthetic-main', 'API_BASE': 'https://primary.example.invalid/v1', 'API_KEY': 'synthetic-main',
                  'FALLBACK_MODELS': ['openai/synthetic-backup'], 'FALLBACK_API_BASE': 'https://backup.example.invalid/v1',
                  'FALLBACK_API_KEY': 'synthetic-backup'}
        self.assertEqual(self._run_transport(config, handler), 'offline answer')
        self.assertEqual([host for host, _ in attempts], ['primary.example.invalid'] * expected + ['backup.example.invalid'])
        self.assertEqual([auth for _, auth in attempts], ['Bearer synthetic-main'] * expected + ['Bearer synthetic-backup'])

    def test_http_408_is_one_http_attempt_before_backup(self):
        self._retry(408, 1)

    def test_transport_timeout_is_one_http_attempt_before_backup(self):
        self._retry('timeout', 1)

    def test_http_503_is_three_http_attempts_not_nine(self):
        self._retry(503, 3)

    def test_default_openai_endpoint_ignores_sdk_environment_override(self):
        urls = []
        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(200, json=self._response())
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://ambient.example.invalid/v1', 'OPENAI_API_BASE': 'https://other.example.invalid/v1'}):
            self.assertEqual(self._run_transport({'MODEL': 'openai/synthetic', 'API_KEY': 'synthetic-only'}, handler), 'offline answer')
        self.assertEqual(urls, ['https://api.openai.com/v1/chat/completions'])

    def test_gemini_default_is_pinned_and_keeps_the_provider_path(self):
        urls = []
        def post(*args, **kwargs):
            url = kwargs.get('url') or args[0]
            urls.append(str(url))
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': [{'text': 'offline answer'}]}, 'finishReason': 'STOP'}],
                'usageMetadata': {'promptTokenCount': 1, 'candidatesTokenCount': 1, 'totalTokenCount': 2}})
        with patch.dict(os.environ, {'GEMINI_API_BASE': 'https://ambient.example.invalid'}), \
                patch('litellm.llms.custom_httpx.http_handler.HTTPHandler.post', side_effect=post):
            self.assertEqual(AIClient({'MODEL': 'gemini/gemini-2.5-flash', 'API_KEY': 'synthetic-gemini'}).chat(self.messages), 'offline answer')
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith('https://generativelanguage.googleapis.com/'))
        self.assertIn('models/gemini-2.5-flash:generateContent', urls[0])

    def test_sdk_global_auth_and_retry_overrides_are_rejected_before_http(self):
        for name, value in (('headers', {'x-private': 'synthetic'}), ('num_retries', 9), ('proxy_auth', object())):
            with self.subTest(name=name), patch.object(litellm, name, value), self.assertRaisesRegex(ValueError, 'SDK 全局'):
                AIClient({'MODEL': 'openai/synthetic', 'API_KEY': 'synthetic'}).chat(self.messages)


    def test_extra_body_cannot_override_model_messages_or_retry_policy(self):
        for field in ('model', 'messages', 'max_retries', 'num_retries', 'timeout'):
            client = AIClient({'MODEL': 'openai/synthetic', 'API_KEY': 'synthetic',
                               'EXTRA_PARAMS': {'extra_body': {field: 'synthetic-private'}}})
            with self.subTest(field=field):
                valid, error = client.validate_config()
                self.assertFalse(valid)
                self.assertNotIn('synthetic-private', error)
                with self.assertRaisesRegex(ValueError, '受控字段'):
                    client.chat(self.messages)


    def test_cf27b_missing_body_gets_thinking_disabled(self):
        params = {'timeout': 120, 'api_base': 'https://relay.example.invalid/v1'}
        actual = _model_request_params('openai/@cf/qwen/qwen3.8-27b', params)
        self.assertEqual(actual['extra_body'], {'chat_template_kwargs': {'enable_thinking': False}})
        self.assertNotIn('extra_body', params)
        self.assertEqual(actual['api_base'], params['api_base'])

    def test_cf27b_preserves_fields_without_mutating_nested_input(self):
        params = {'extra_body': {'vendor_options': {'items': [1]}, 'chat_template_kwargs': {
            'enable_thinking': True, 'other_setting': {'items': [2]}}}}
        before = deepcopy(params)
        actual = _model_request_params('openai/@cf/qwen/qwen3.8-27b', params)
        self.assertIs(actual['extra_body']['chat_template_kwargs']['enable_thinking'], False)
        self.assertEqual(actual['extra_body']['vendor_options'], {'items': [1]})
        self.assertEqual(actual['extra_body']['chat_template_kwargs']['other_setting'], {'items': [2]})
        actual['extra_body']['vendor_options']['items'].append(3)
        actual['extra_body']['chat_template_kwargs']['other_setting']['items'].append(4)
        self.assertEqual(params, before)

    def test_cf27b_exact_match_leaves_other_models_unchanged(self):
        models = ('gemini/gemini-3.5-flash', 'openai/another-model',
                  'openai/@cf/qwen/qwen3-30b-a3b-fp8', 'openai/@cf/qwen/qwen3.8-27b-other')
        for model in models:
            for params in ({'timeout': 120}, {'extra_body': {'chat_template_kwargs': {'enable_thinking': True}}}):
                before = deepcopy(params)
                with self.subTest(model=model, supplied_body='extra_body' in params):
                    self.assertIs(_model_request_params(model, params), params)
                    self.assertEqual(params, before)

    def test_cf27b_invalid_nested_settings_fail_before_http_and_retry(self):
        for field, value in (('extra_body', 'synthetic-private'), ('extra_body', []),
                             ('chat_template_kwargs', 'synthetic-private'), ('chat_template_kwargs', []),
                             ('chat_template_kwargs', False)):
            extra = {field: value} if field == 'extra_body' else {'extra_body': {field: value}}
            client = AIClient({'MODEL': 'openai/@cf/qwen/qwen3.8-27b', 'API_KEY': 'synthetic',
                               'API_BASE': 'https://relay.example.invalid/v1', 'EXTRA_PARAMS': extra})
            with self.subTest(field=field, kind=type(value).__name__), \
                    patch('trendradar.ai.client.completion') as completion, patch('time.sleep') as sleep:
                with self.assertRaises(ValueError) as error:
                    client.chat(self.messages)
                self.assertNotIn('synthetic-private', str(error.exception))
                completion.assert_not_called()
                sleep.assert_not_called()

    def test_cf27b_rule_applies_only_to_target_at_each_chain_position(self):
        class SyntheticTimeout(Exception):
            status_code = 408
        target = 'openai/@cf/qwen/qwen3.8-27b'
        for position in range(3):
            models = ['openai/one', 'openai/two', 'openai/three']
            models[position] = target
            models.append('openai/final')
            params_seen = []
            def completion(**params):
                params_seen.append(deepcopy(params))
                if len(params_seen) < len(models):
                    raise SyntheticTimeout('synthetic timeout')
                return self._response()
            config = {'MODEL': models[0], 'API_BASE': 'https://relay.example.invalid/v1', 'API_KEY': 'synthetic',
                      'FALLBACK_MODELS': models[1:], 'FALLBACK_API_BASE': '@'.join(['https://relay.example.invalid/v1'] * 3)}
            with self.subTest(position=position), patch('trendradar.ai.client.completion', side_effect=completion):
                self.assertEqual(AIClient(config).chat(self.messages), 'offline answer')
            self.assertEqual([p['model'] for p in params_seen], models)
            for params in params_seen:
                if params['model'] == target:
                    self.assertIs(params['extra_body']['chat_template_kwargs']['enable_thinking'], False)
                else:
                    self.assertNotIn('extra_body', params)
                self.assertEqual((params['max_retries'], params['num_retries']), (0, 0))

    def test_cf27b_retry_rebuilds_body_after_sdk_mutation(self):
        class SyntheticUnavailable(Exception):
            status_code = 503
        body = {'chat_template_kwargs': {'enable_thinking': True, 'kept': 'value'}, 'nested': {'items': [1]}}
        original = deepcopy(body)
        client = AIClient({'MODEL': 'openai/@cf/qwen/qwen3.8-27b', 'API_KEY': 'synthetic',
                           'API_BASE': 'https://relay.example.invalid/v1', 'EXTRA_PARAMS': {'extra_body': body}})
        seen = []
        def completion(**params):
            seen.append(deepcopy(params['extra_body']))
            params['extra_body']['chat_template_kwargs']['enable_thinking'] = True
            params['extra_body']['nested']['items'].append(2)
            if len(seen) == 1:
                raise SyntheticUnavailable('synthetic overload')
            return self._response()
        with patch('trendradar.ai.client.completion', side_effect=completion):
            self.assertEqual(client.chat(self.messages), 'offline answer')
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertIs(seen[0]['chat_template_kwargs']['enable_thinking'], False)
        self.assertEqual(seen[0]['chat_template_kwargs']['kept'], 'value')
        self.assertEqual(body, original)
        self.assertEqual(client.extra_params['extra_body'], original)

    def test_cf27b_keyword_derivation_uses_same_rule_without_polluting_parent(self):
        parent = AIClient({'MODEL': 'gemini/gemini-3.5-flash', 'API_KEY': 'synthetic-gemini',
                           'FALLBACK_MODELS': ['openai/@cf/qwen/qwen3.8-27b'],
                           'FALLBACK_API_BASE': 'https://relay.example.invalid/v1', 'FALLBACK_API_KEY': 'synthetic-relay'})
        keyword = build_keyword_client(parent)
        with patch('trendradar.ai.client.completion', return_value=self._response()) as completion:
            self.assertEqual(keyword.chat(self.messages), 'offline answer')
        self.assertIs(completion.call_args.kwargs['extra_body']['chat_template_kwargs']['enable_thinking'], False)
        self.assertEqual(keyword.last_candidate_id, 'fallback:1')
        self.assertEqual(parent.extra_params, {})
        self.assertIsNone(parent.last_model)

    def test_cf27b_final_http_json_is_disabled_on_retry_and_not_on_neighbors(self):
        seen = []
        cf_attempts = 0
        target = '@cf/qwen/qwen3.8-27b'
        def handler(request):
            nonlocal cf_attempts
            payload = json.loads(request.content)
            seen.append((request.url.host, payload))
            if payload['model'] == 'before':
                return httpx.Response(408, json={'error': {'message': 'synthetic timeout'}})
            if payload['model'] == target:
                cf_attempts += 1
                return httpx.Response(503 if cf_attempts == 1 else 408, json={'error': {'message': 'synthetic failure'}})
            return httpx.Response(200, json=self._response())
        config = {'MODEL': 'openai/before', 'API_BASE': 'https://relay-a.example.invalid/v1', 'API_KEY': 'synthetic-a',
                  'FALLBACK_MODELS': ['openai/' + target, 'openai/after'],
                  'FALLBACK_API_BASE': 'https://relay-b.example.invalid/v1@https://relay-c.example.invalid/v1',
                  'FALLBACK_API_KEY': 'synthetic-b@synthetic-c',
                  'EXTRA_PARAMS': {'extra_body': {'vendor_marker': 'kept'}}}
        self.assertEqual(self._run_transport(config, handler), 'offline answer')
        self.assertEqual([p['model'] for _, p in seen], ['before', target, target, 'after'])
        self.assertEqual([host for host, _ in seen], ['relay-a.example.invalid', 'relay-b.example.invalid', 'relay-b.example.invalid', 'relay-c.example.invalid'])
        for _, payload in seen:
            self.assertEqual(payload['vendor_marker'], 'kept')
            if payload['model'] == target:
                self.assertIs(payload['chat_template_kwargs']['enable_thinking'], False)
            else:
                self.assertNotIn('chat_template_kwargs', payload)


if __name__ == '__main__':
    unittest.main()
