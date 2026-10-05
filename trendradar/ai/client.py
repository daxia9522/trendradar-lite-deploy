# coding=utf-8
"""基于 LiteLLM 的统一 AI 客户端。"""

from __future__ import annotations

import random
import time
from copy import copy, deepcopy
from typing import Any, Dict, List, Optional

from trendradar.ai_config import AICandidate, build_candidates, same_endpoint

try:
    # Keep non-AI commands (doctor, config validation, storage maintenance)
    # usable when the optional runtime dependency is not installed.  The
    # scheduled AI path still fails explicitly at the point where a request
    # is attempted instead of making every TrendRadar import fail.
    from litellm import completion
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by lean installs
    if exc.name != "litellm":
        raise
    completion = None

# 客户端默认值（config.yaml 只保留 timeout；重试次数不进配置）
DEFAULT_TIMEOUT = 120
DEFAULT_NUM_RETRIES = 2

# 拥塞类错误（5xx/429）是快速拒绝，用指数退避等待恢复窗口；
# 退避档数同时决定此类错误的额外重试机会（首尝试 + 档数）。
# 408 超时是确定性慢（历史 6 次同模型超时重试 0 成功），
# 单模型超时最多试 TIMEOUT_RETRY_LIMIT 次，失败立即切备用，不烧退避。
RETRY_BACKOFF_SECONDS = (5, 8)
RETRY_BACKOFF_STATUSES = {429, 500, 502, 503, 504}
TIMEOUT_RETRY_LIMIT = 1


def _is_timeout_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status == 408 or type(exc).__name__ == "Timeout"


def _response_content(response: Any) -> str:
    """兼容 LiteLLM/OpenAI 返回的字符串或内容块列表。"""
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    choices = choices or []
    if not choices:
        return ""
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message")
    content = getattr(message, "content", "") if message is not None else ""
    if isinstance(message, dict):
        content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            item.get("text", str(item)) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _finish_reason(response: Any) -> Optional[str]:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    choices = choices or []
    if not choices:
        return None
    reason = getattr(choices[0], "finish_reason", None)
    if reason is None and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
    return str(reason).lower() if reason is not None else None


# Routing and retry policy belong to the candidate/client, not a free-form body.
CONTROLLED_PARAMS = frozenset({
    "model", "api_key", "api_base", "base_url", "custom_llm_provider",
    "deployment_id", "azure", "model_list", "fallbacks", "context_window_fallback_dict",
    "client", "max_retries", "retry_policy", "messages",
})


def _check_request_params(params, *, extra=False):
    forbidden = CONTROLLED_PARAMS | ({"timeout", "num_retries"} if extra else set())
    found = sorted(forbidden.intersection(params))
    if found:
        raise ValueError("AI 请求参数不能覆盖受控字段：" + ", ".join(found))
    body = params.get("extra_body")
    if body is not None:
        if not isinstance(body, dict):
            raise ValueError("AI extra_body 必须是对象")
        body_fields = sorted((CONTROLLED_PARAMS | {"timeout", "num_retries"}).intersection(body))
        if body_fields:
            raise ValueError("AI extra_body 不能覆盖受控字段：" + ", ".join(body_fields))
    if params.get("stream"):
        raise ValueError("AIClient 只支持非流式响应")


def _model_request_params(model: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Force thinking off for the exact CF 27B model, on a fresh attempt copy."""
    if model != "openai/@cf/qwen/qwen3.8-27b":
        return params
    body = params.get("extra_body")
    if body is not None and not isinstance(body, dict):
        raise ValueError("CF Qwen 27B extra_body 必须是对象")
    extra_body = deepcopy(body) if body is not None else {}
    template = extra_body.get("chat_template_kwargs")
    if template is not None and not isinstance(template, dict):
        raise ValueError("CF Qwen 27B chat_template_kwargs 必须是对象")
    template = dict(template) if template is not None else {}
    template["enable_thinking"] = False
    request_params = dict(params)
    request_params["extra_body"] = {**extra_body, "chat_template_kwargs": template}
    return request_params


def _check_sdk_globals():
    # SDK-global headers/proxy auth can otherwise reappear after slot isolation.
    import litellm
    if any(getattr(litellm, name, None) for name in ("headers", "proxy_auth", "num_retries")):
        raise ValueError("AIClient 不接受 SDK 全局鉴权头、代理鉴权或重试覆盖")


class AIClient:
    """统一 AI 客户端，保留项目现有的 ``chat(messages)`` 接口。"""

    def __init__(self, config: Dict[str, Any]):
        resolved = build_candidates(config)
        self.candidates = resolved.candidates
        self.configuration_error = resolved.error
        self._origin_candidate = self.candidates[0]
        self._keyword_candidate = next((c for c in self.candidates if c.id == "fallback:1"), self.candidates[0])
        self.model = self.candidates[0].model
        self.api_key = self.candidates[0].api_key
        self.api_base = self.candidates[0].api_base
        self.fallback_models = [c.model for c in self.candidates[1:]]
        self.timeout = config.get("TIMEOUT", DEFAULT_TIMEOUT)
        self.num_retries = int(config.get("NUM_RETRIES", DEFAULT_NUM_RETRIES) or 0)
        self.extra_params = deepcopy(dict(config.get("EXTRA_PARAMS") or {}))
        self.last_finish_reason: Optional[str] = None
        self.last_model: Optional[str] = None
        self.last_candidate_id: Optional[str] = None

    def _model_chain(self) -> tuple[AICandidate, ...]:
        return self.candidates

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """调用主模型；失败时按顺序尝试备用模型。

        不主动传 temperature / max_tokens：当前中转站与新模型会忽略它们。
        确实需要时（如换回 Gemini 官方）可由调用处通过 kwargs 临时传入。
        """
        ok, error = self.validate_config()
        if not ok:
            raise ValueError(error)
        if completion is None:
            raise RuntimeError(
                "LiteLLM 未安装，无法调用 AI；请安装项目锁定依赖 requirements.lock"
            )

        _check_request_params(self.extra_params, extra=True)
        _check_request_params(kwargs)
        _check_sdk_globals()
        timeout = kwargs.pop("timeout", self.timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("AI timeout 必须是正数")
        num_retries = int(kwargs.pop("num_retries", self.num_retries) or 0)
        params: Dict[str, Any] = dict(self.extra_params)
        params.update(kwargs)
        params.update(messages=messages, timeout=timeout, max_retries=0, num_retries=0)

        models = self._model_chain()
        last_error: Optional[Exception] = None
        self.last_model = None
        self.last_finish_reason = None
        self.last_candidate_id = None
        for index, candidate in enumerate(models):
            model = candidate.model
            slot_log = f", candidate={candidate.id}" if candidate.id != "main" else ""
            if candidate.error:
                print(f"[AI] 跳过模型 {model}: {candidate.error}{slot_log}")
                continue
            model_params = dict(params)
            if "extra_body" in model_params:
                model_params["extra_body"] = deepcopy(model_params["extra_body"])
            # Same provider does not imply the same endpoint or credential.
            if (not same_endpoint(candidate, self._origin_candidate)
                    or candidate.api_key != self._origin_candidate.api_key):
                model_params.pop("headers", None)
                model_params.pop("extra_headers", None)
                model_params.pop("provider_specific_header", None)
            model_params["api_key"] = candidate.api_key
            model_params["api_base"] = candidate.request_base
            base_attempts = max(1, num_retries + 1)
            slow_delays_left = list(RETRY_BACKOFF_SECONDS)
            attempt = 0
            while True:
                attempt += 1
                request_params = _model_request_params(model, model_params)
                timeout = request_params["timeout"]
                print(
                    f"[AI] 请求开始: model={model}, "
                    f"attempt={attempt}, timeout={timeout}s{slot_log}"
                )
                try:
                    response = completion(model=model, **request_params)
                    self.last_model = model
                    self.last_finish_reason = _finish_reason(response)
                    self.last_candidate_id = candidate.id
                    return _response_content(response)
                except Exception as exc:
                    last_error = exc
                    # 仅记录异常类名与 HTTP 状态码；不输出异常正文（可能含 URL/密钥）
                    status = getattr(exc, "status_code", None)
                    detail = type(exc).__name__ + (f"({status})" if status else "")
                    print(f"[AI] attempt 失败: {detail}")
                    if status in RETRY_BACKOFF_STATUSES and slow_delays_left:
                        delay = slow_delays_left.pop(0)
                    elif _is_timeout_error(exc):
                        # 超时单模型最多 TIMEOUT_RETRY_LIMIT 次，立即让出机会给备用
                        if attempt >= TIMEOUT_RETRY_LIMIT:
                            break
                        delay = 1
                    elif attempt < base_attempts:
                        delay = min(2 ** (attempt - 1), 8)
                    else:
                        break
                    # 0~1s 抖动，避免多个定时任务同拍重试
                    time.sleep(delay + random.uniform(0, 1))
            if index + 1 < len(models):
                print(f"[AI] 模型 {model} 调用失败，尝试备用模型")

        if last_error is None:
            raise ValueError("AI 模型链中没有可用的鉴权配置")
        raise last_error

    def validate_config(self) -> tuple[bool, str]:
        if self.configuration_error:
            return False, self.configuration_error
        active = self.candidates[0]
        if active.error:
            return False, active.error
        try:
            _check_request_params(self.extra_params, extra=True)
        except ValueError as error:
            return False, str(error)
        return True, ""

    def configuration_warnings(self) -> list[str]:
        return [f"{c.id}: {c.error}" for c in self.candidates[1:] if c.error]


def build_keyword_client(ai_config: Dict[str, Any] | AIClient) -> AIClient:
    """从同一份 AI 配置派生关键词客户端。

    便宜模型 = FALLBACK_MODELS 首项；未配置时回退主模型。
    """
    # Bind the complete original slot, not just a new model name/provider.
    client = copy(ai_config) if isinstance(ai_config, AIClient) else AIClient(ai_config or {})
    selected = client._keyword_candidate
    client.candidates = (selected,)
    client.model = selected.model
    client.api_key = selected.api_key
    client.api_base = selected.api_base
    client.fallback_models = []
    client.extra_params = dict(client.extra_params)
    client.num_retries = 1
    client.last_model = None
    client.last_finish_reason = None
    client.last_candidate_id = None
    return client
