# coding=utf-8
"""One LiteLLM transport; task-local candidates and bounded analysis retry policy."""
from __future__ import annotations

import os
import random
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional

from trendradar.ai_config import AICandidate, validate_request_params

try:
    from litellm import completion
except ModuleNotFoundError as exc:
    if exc.name != 'litellm':
        raise
    completion = None

# Preserve production's status-based classification, including HTTP 429.
# Do not invent a separate quota/body-text classifier or terminal 429 policy.
CONGESTION_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_timeout_error(exc: Exception) -> bool:
    return getattr(exc, 'status_code', None) == 408 or type(exc).__name__ == 'Timeout'


def _response_content(response: Any) -> str:
    choices = getattr(response, 'choices', None)
    if choices is None and isinstance(response, dict):
        choices = response.get('choices')
    if not choices:
        return ''
    choice = choices[0]
    message = getattr(choice, 'message', None)
    if message is None and isinstance(choice, dict):
        message = choice.get('message')
    content = getattr(message, 'content', '') if message is not None else ''
    if isinstance(message, dict):
        content = message.get('content', '')
    if isinstance(content, list):
        return '\n'.join(item.get('text', str(item)) if isinstance(item, dict) else str(item)
                         for item in content)
    return str(content or '')


def _finish_reason(response: Any) -> Optional[str]:
    choices = getattr(response, 'choices', None)
    if choices is None and isinstance(response, dict):
        choices = response.get('choices')
    if not choices:
        return None
    reason = getattr(choices[0], 'finish_reason', None)
    if reason is None and isinstance(choices[0], dict):
        reason = choices[0].get('finish_reason')
    return str(reason).lower() if reason is not None else None


def _check_sdk_globals():
    import litellm
    if any(getattr(litellm, name, None) for name in
           ('headers', 'proxy_auth', 'num_retries', 'api_base', 'api_key',
            'use_litellm_proxy', 'model_alias_map', 'client_session', 'aclient_session')):
        raise ValueError('AIClient 不接受 SDK 全局路由、鉴权或重试覆盖')
    # An empty model_fallbacks list can still enter the SDK fallback branch.
    if any(getattr(litellm, name, None) is not None for name in ('model_fallbacks', 'fallbacks')):
        raise ValueError('备用切换由任务负责，不接受 SDK 全局备用链')
    if any(os.environ.get(name, '').strip() for name in
           ('GEMINI_API_BASE', 'GOOGLE_API_BASE', 'OPENAI_API_BASE', 'OPENAI_BASE_URL',
            'LITELLM_PROXY_API_BASE')):
        raise ValueError('请移除 SDK 全局端点覆盖，接口只由 model_groups 指定')
    proxy = os.environ.get('USE_LITELLM_PROXY', '').strip().lower()
    if proxy not in ('', 'false', '0', 'no', 'off'):
        raise ValueError('不允许通过 SDK 全局代理绕过连接组')
    if (str(getattr(litellm, 'ssl_verify', True)).strip().lower() in ('false', '0', 'no', 'off')
            or os.environ.get('SSL_VERIFY', '').strip().lower() in ('false', '0', 'no', 'off')):
        raise ValueError('不允许关闭模型请求的 TLS 证书校验')


class AIClient:
    """Consumes one resolved task, never guesses its connection or reasoning settings."""

    def __init__(self, config: Dict[str, Any]):
        self.task_name = config.get('TASK_NAME', '')
        self.candidates = deepcopy(tuple(config.get('CANDIDATES', ())))
        self.timeout = config.get('TIMEOUT')
        self.retry = deepcopy(config.get('RETRY', {}))
        self.model = self.candidates[0].model if self.candidates else ''
        self.api_key = self.candidates[0].api_key if self.candidates else ''
        self.api_base = self.candidates[0].api_base if self.candidates else ''
        self.last_finish_reason = None
        self.last_model = None
        self.last_candidate_id = None

    def validate_config(self) -> tuple[bool, str]:
        if not self.task_name or not self.candidates:
            return False, '缺少已解析的 AI 任务与候选模型'
        for candidate in self.candidates:
            if not isinstance(candidate, AICandidate):
                return False, '候选模型必须来自配置解析器'
            if candidate.error or not candidate.api_key:
                return False, candidate.error or f'模型 {candidate.id} 缺少凭据'
            try:
                validate_request_params(candidate.request_params)
            except ValueError as exc:
                return False, str(exc)
        return True, ''

    def configuration_warnings(self) -> list[str]:
        return [c.error for c in self.candidates if c.error]

    def call_once(self, candidate_id: str, messages: List[Dict[str, str]], *, timeout=None) -> str:
        """Call one member of this task's validated chain exactly once. No retry/fallback."""
        self.last_model = self.last_finish_reason = self.last_candidate_id = None
        valid, error = self.validate_config()
        if not valid:
            raise ValueError(error)
        if completion is None:
            raise RuntimeError('LiteLLM 未安装；请安装项目锁定依赖 requirements.lock')
        _check_sdk_globals()
        candidate = next((c for c in self.candidates if c.id == candidate_id), None)
        if candidate is None:
            raise ValueError('候选模型不属于当前任务')
        actual_timeout = self.timeout if timeout is None else timeout
        if type(actual_timeout) not in (int, float) or not 0 < actual_timeout <= 3600:
            raise ValueError('AI timeout 必须为 0..3600 范围的正数')
        # Each call starts clean: no previous candidate's extra_body, key or headers.
        params = validate_request_params(candidate.request_params)
        params.update(messages=deepcopy(messages), api_key=candidate.api_key,
                      timeout=actual_timeout, max_retries=0, num_retries=0)
        if candidate.api_base:
            params['api_base'] = candidate.api_base
        response = completion(model=candidate.model, **params)
        # Full request identifier, not the registry alias or an untrusted response label.
        self.last_model = candidate.model
        self.last_candidate_id = candidate.id
        self.last_finish_reason = _finish_reason(response)
        return _response_content(response)

    def chat(self, messages: List[Dict[str, str]], *, timeout=None) -> str:
        """Analysis/keyword request policy. Classifier owns its own whole-batch budget."""
        if self.task_name == 'ai_filter':
            raise ValueError('筛选必须使用单次候选调用，由分类器统一管理尝试预算')
        valid, error = self.validate_config()
        if not valid:
            raise ValueError(error)
        normal_attempts = self.retry['normal_retries'] + 1
        last_error = None
        for index, candidate in enumerate(self.candidates):
            slow_delays_left = list(self.retry['congestion_delays'])
            attempt = 0
            while True:
                attempt += 1
                print(f'[AI] 请求开始: model={candidate.model}, attempt={attempt}, '
                      f'timeout={self.timeout if timeout is None else timeout}s, candidate={candidate.id}')
                try:
                    return self.call_once(candidate.id, messages, timeout=timeout)
                except Exception as exc:
                    last_error = exc
                    status = getattr(exc, 'status_code', None)
                    detail = type(exc).__name__ + (f'({status})' if type(status) is int else '')
                    print(f'[AI] attempt 失败: {detail}')
                    if status in CONGESTION_STATUSES and slow_delays_left:
                        delay = slow_delays_left.pop(0)
                    elif _is_timeout_error(exc):
                        if attempt >= self.retry['timeout_retries'] + 1:
                            break
                        delay = 1
                    elif attempt < normal_attempts:
                        delay = min(2 ** (attempt - 1), 8)
                    else:
                        break
                    time.sleep(delay + random.uniform(0, 1))
            if index + 1 < len(self.candidates):
                print(f'[AI] 模型 {candidate.model} 调用失败，尝试备用模型')
        if last_error is None:
            raise ValueError('AI 任务没有可用候选模型')
        raise last_error
