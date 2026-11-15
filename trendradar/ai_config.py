# coding=utf-8
"""Named connections and task-local model choices; no positional credential lists.

Parsing is value-safe. Only an injected loader callback may open a secret file.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import math
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import yaml

TASK_NAMES = frozenset({'ai_filter', 'daily_analysis', 'weekly_analysis', 'weekly_keywords'})
BODY_TASKS = frozenset({'daily_analysis', 'weekly_analysis'})
LEGACY_FIELDS = ('MODEL', 'API_BASE', 'API_KEY', 'API_KEY_FILE', 'FALLBACK_MODELS',
                 'FALLBACK_API_BASE', 'FALLBACK_API_KEY', 'FALLBACK_API_KEY_FILE', 'TIMEOUT',
                 'EXTRA_PARAMS', 'NUM_RETRIES')
CONTROLLED_PARAMS = frozenset({
    'model', 'api_key', 'api_base', 'base_url', 'custom_llm_provider', 'deployment_id',
    'azure', 'model_list', 'fallbacks', 'context_window_fallback_dict', 'client',
    'max_retries', 'num_retries', 'retry_policy', 'messages', 'single_attempt',
    'timeout', 'model_id', 'candidate_id', 'analysis_defaults', 'stream',
    'headers', 'extra_headers', 'provider_specific_header', 'litellm_params',
    'use_litellm_proxy', 'ssl_verify', 'ssl_cert', 'ssl_ca_cert', 'ssl_certificate',
    'model_alias_map', 'model_fallbacks', 'client_session', 'aclient_session',
    'cache', 'caching', 'cache_key', 'preset_cache_key',
})
# This is a generation-parameter boundary, not a free-form LiteLLM kwargs tunnel.
# Provider-specific generation options remain possible through extra_body.
GENERATION_PARAMS = frozenset({
    'reasoning_effort', 'thinking', 'temperature', 'top_p', 'top_k',
    'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'seed', 'stop', 'n',
    'presence_penalty', 'frequency_penalty', 'repetition_penalty', 'logit_bias',
    'logprobs', 'top_logprobs', 'response_format', 'extra_body',
})
_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]*\Z')
_ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate mappings rather than silently overwriting a model/secret ref."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise ValueError('YAML 配置键必须可哈希') from None
        if duplicate:
            raise ValueError('YAML 配置包含重复键')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_yaml_config(text: str) -> dict:
    try:
        data = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError:
        raise ValueError('YAML 配置格式错误') from None
    if not isinstance(data, dict):
        raise ValueError('配置顶层必须是对象')
    return data


def valid_model(model: str) -> bool:
    provider, separator, actual = model.partition('/')
    return bool(separator and actual and re.fullmatch(r'[A-Za-z0-9_-]+', provider)
                and not any(char.isspace() or ord(char) < 32 for char in model))


def api_base_error(base: str) -> str:
    if not base:
        return ''
    try:
        parts = urlsplit(base)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.fragment or any(char.isspace() or ord(char) < 32 for char in base)):
            raise ValueError
        _ = parts.port
    except ValueError:
        return 'API 地址必须是无用户凭据及片段的合法 HTTP(S) 地址'
    return ''


def validate_request_params(params: Any) -> dict:
    if not isinstance(params, dict):
        raise ValueError('request_params 必须为对象')
    if set(params) - GENERATION_PARAMS:
        raise ValueError('request_params 只接受生成参数，不接受 SDK 路由、传输或模拟控制')
    # Validate recursively: a nested SDK routing override is not a generation option.
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (not isinstance(key, str) or key in CONTROLLED_PARAMS
                        or key.startswith('mock_')):
                    raise ValueError('request_params 不得覆盖连接、传输、模拟响应或重试控制字段')
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif value is not None and type(value) not in (str, bool, int, float):
            raise ValueError('request_params 仅允许 JSON 值')
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError('request_params 不允许非有限数值')
    visit(params)
    if 'extra_body' in params and not isinstance(params['extra_body'], dict):
        raise ValueError('extra_body 必须为对象')
    return deepcopy(params)


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} 必须为对象')
    return value


def _fields(value, allowed, label):
    if set(value) - set(allowed):
        raise ValueError(f'{label} 包含未知配置字段')


def _number(value, label, *, maximum=3600, integer=False, minimum=0):
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f'{label} 不在允许的数值范围')
    return value


@dataclass(frozen=True)
class AICandidate:
    id: str
    model: str
    connection: str
    api_base: str = field(default='', repr=False)
    api_key: str = field(default='', repr=False)
    request_params: dict = field(default_factory=dict, repr=False)
    error: str = ''

    def request_identity(self):
        """Aliases and credentials do not affect classification cache identity."""
        return {'model': self.model, 'api_base': self.api_base,
                'request_params': deepcopy(self.request_params)}


class ModelRegistry:
    """One validated config snapshot. No network or file I/O in this class."""

    def __init__(self, data: Mapping[str, Any], env: Mapping[str, str]):
        if 'ai' in data:
            raise ValueError('旧 ai 配置已退役；请迁移到 model_groups 和 tasks')
        filter_section = _object(data.get('ai_filter', {}), 'ai_filter')
        if set(filter_section) - {'enabled', 'min_score'}:
            raise ValueError('ai_filter 仅保留功能开关和 min_score；模型配置请使用 tasks.ai_filter')
        for prefix in ('AI_', 'AI_FILTER_'):
            if any(str(env.get(prefix + name, '')).strip() for name in LEGACY_FIELDS):
                raise ValueError('发现旧 AI 模型/连接环境覆盖；请移除分隔符配置并迁移连接组')
        self.env = dict(env)
        self.groups = deepcopy(_object(data.get('model_groups'), 'model_groups'))
        self.tasks = deepcopy(_object(data.get('tasks'), 'tasks'))
        if not self.groups or set(self.tasks) != TASK_NAMES:
            raise ValueError('model_groups 不能为空，tasks 必须明确包含四个任务')
        self.models = {}
        self._keys = {}
        self._bases = {}
        for ident, group in self.groups.items():
            if not isinstance(ident, str) or not _ID.fullmatch(ident):
                raise ValueError('连接组 ID 格式错误')
            _object(group, '连接组')
            _fields(group, {'api_base', 'api_base_env', 'api_key_env', 'api_key_file_env', 'models'}, '连接组')
            refs = [k for k in ('api_key_env', 'api_key_file_env') if k in group]
            if len(refs) != 1 or not isinstance(group[refs[0]], str) or not _ENV.fullmatch(group[refs[0]]):
                raise ValueError('连接组须且仅须配置一个有效凭据引用')
            base = group.get('api_base', '')
            if 'api_base_env' in group:
                ref = group['api_base_env']
                if 'api_base' in group or not isinstance(ref, str) or not _ENV.fullmatch(ref):
                    raise ValueError('连接组地址须使用 api_base 或有效的 api_base_env 引用，不能同时配置')
                base = self.env.get(ref, '')
                if not isinstance(base, str) or not base:
                    raise ValueError(f'连接组 {ident} 的地址环境变量缺失或为空')
            if not isinstance(base, str) or api_base_error(base):
                raise ValueError('连接组 API 地址无效')
            self._bases[ident] = base
            models = _object(group.get('models'), '组内 models')
            if not models:
                raise ValueError('组内 models 不能为空')
            providers = set()
            for alias, model in models.items():
                if not isinstance(alias, str) or not _ID.fullmatch(alias) or alias in self.models:
                    raise ValueError('模型 ID 必须格式有效且全局唯一')
                if not isinstance(model, str) or not valid_model(model):
                    raise ValueError('模型标识必须为合法 provider/model')
                providers.add(model.partition('/')[0])
                self.models[alias] = (ident, model)
            if len(providers) != 1:
                raise ValueError('同一连接组内模型必须使用同一调用协议')
            if not base and providers != {'gemini'}:
                raise ValueError('非 Gemini 原生连接组必须明确填写 api_base 或 api_base_env')
        # All task structure is checked, but unused groups do not require credentials.
        for name, task in self.tasks.items():
            _object(task, '任务')
            _fields(task, {'timeout', 'retry', 'model_chain'}, '任务')
            _number(task.get('timeout'), 'timeout', minimum=1)
            chain = task.get('model_chain')
            if not isinstance(chain, list) or not chain or len(chain) > 16:
                raise ValueError('model_chain 必须为非空候选列表（最多16项）')
            if name == 'weekly_keywords' and len(chain) != 1:
                raise ValueError('周报关键词必须明确配置一个模型，无备用链')
            seen = set()
            for node in chain:
                _object(node, '模型节点')
                _fields(node, {'model_id', 'request_params'}, '模型节点')
                alias = node.get('model_id')
                if not isinstance(alias, str) or alias not in self.models or alias in seen:
                    raise ValueError('任务引用未知或重复的模型 ID')
                seen.add(alias)
                validate_request_params(node.get('request_params', {}))
            retry = _object(task.get('retry'), 'retry')
            if name == 'ai_filter':
                _fields(retry, {'attempts_per_model', 'delay_seconds'}, '筛选 retry')
                _number(retry.get('attempts_per_model'), 'attempts_per_model', integer=True, minimum=1, maximum=10)
                _number(retry.get('delay_seconds'), 'delay_seconds', maximum=300)
            else:
                _fields(retry, {'normal_retries', 'congestion_delays', 'timeout_retries'}, 'retry')
                for key in ('normal_retries', 'timeout_retries'):
                    _number(retry.get(key), key, integer=True, maximum=10)
                delays = retry.get('congestion_delays')
                if not isinstance(delays, list) or len(delays) > 10:
                    raise ValueError('congestion_delays 必须为有界等待列表')
                for delay in delays:
                    _number(delay, 'congestion delay', maximum=300)

    def resolve_task(self, name: str, read_secret_file: Callable[[str], str], *, model_id=None) -> dict:
        if name not in self.tasks:
            raise ValueError('未知 AI 任务')
        task = deepcopy(self.tasks[name])
        chain = task['model_chain']
        if model_id is not None:
            # A CLI override selects an already configured task node, including params.
            # Moving to another connection never inherits the old node's credentials.
            chain = [node for node in chain if node['model_id'] == model_id]
            if not chain:
                raise ValueError('模型覆盖须为该任务已配置的 model_id')
        candidates = []
        for node in chain:
            alias = node['model_id']
            group_id, model = self.models[alias]
            group = self.groups[group_id]
            if group_id not in self._keys:
                try:
                    if 'api_key_env' in group:
                        value = str(self.env.get(group['api_key_env'], '')).strip()
                    else:
                        path = str(self.env.get(group['api_key_file_env'], '')).strip()
                        value = read_secret_file(path) if path else ''
                    self._keys[group_id] = (value, '' if value else f'连接组 {group_id} 缺少凭据')
                except (OSError, ValueError, UnicodeError):
                    self._keys[group_id] = ('', f'连接组 {group_id} 的凭据文件无法安全读取')
            key, error = self._keys[group_id]
            candidates.append(AICandidate(alias, model, group_id, self._bases[group_id],
                                          key, validate_request_params(node.get('request_params', {})), error))
        # Uppercase keys are an internal loader contract, not a second user config.
        return {'TASK_NAME': name, 'MODEL': candidates[0].model, 'CANDIDATES': tuple(candidates),
                'TIMEOUT': task['timeout'], 'RETRY': task['retry']}
