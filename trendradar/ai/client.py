# coding=utf-8
"""基于 LiteLLM 的统一 AI 客户端。"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence

from litellm import completion

# 客户端默认值（config.yaml 只保留 timeout；重试次数不进配置）
DEFAULT_TIMEOUT = 240
DEFAULT_NUM_RETRIES = 2


def _normalize_model(model: Any) -> str:
    return str(model or "").strip()


def _normalize_model_list(models: Optional[Sequence[Any]]) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in models or []:
        model = _normalize_model(item)
        if model and model not in seen:
            seen.add(model)
            result.append(model)
    return result


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


class AIClient:
    """统一 AI 客户端，保留项目现有的 ``chat(messages)`` 接口。"""

    def __init__(self, config: Dict[str, Any]):
        self.model = _normalize_model(config.get("MODEL"))
        self.api_key = config.get("API_KEY") or os.environ.get("AI_API_KEY", "")
        self.api_base = str(config.get("API_BASE") or "").strip()
        self.timeout = config.get("TIMEOUT", DEFAULT_TIMEOUT)
        self.num_retries = int(config.get("NUM_RETRIES", DEFAULT_NUM_RETRIES) or 0)
        self.fallback_models = _normalize_model_list(config.get("FALLBACK_MODELS", []))
        self.extra_params = dict(config.get("EXTRA_PARAMS") or {})
        self.last_finish_reason: Optional[str] = None
        self.last_model: Optional[str] = None

    def _model_chain(self) -> List[str]:
        return _normalize_model_list([self.model] + self.fallback_models)

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """调用主模型；失败时按顺序尝试备用模型。

        不主动传 temperature / max_tokens：当前中转站与新模型会忽略它们。
        确实需要时（如换回 Gemini 官方）可由调用处通过 kwargs 临时传入。
        """
        ok, error = self.validate_config()
        if not ok:
            raise ValueError(error)

        params: Dict[str, Any] = {
            "messages": messages,
            "timeout": kwargs.pop("timeout", self.timeout),
        }
        num_retries = int(kwargs.pop("num_retries", self.num_retries) or 0)
        if self.api_key:
            params["api_key"] = self.api_key
        if self.api_base:
            params["api_base"] = self.api_base
        params.update(self.extra_params)
        params.update(kwargs)

        models = self._model_chain()
        last_error: Optional[Exception] = None
        self.last_model = None
        self.last_finish_reason = None
        for index, model in enumerate(models):
            attempts = max(1, num_retries + 1)
            for attempt in range(attempts):
                try:
                    response = completion(model=model, **params)
                    self.last_model = model
                    self.last_finish_reason = _finish_reason(response)
                    return _response_content(response)
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        time.sleep(min(2**attempt, 8))
            if index + 1 < len(models):
                print(f"[AI] 模型 {model} 调用失败，尝试备用模型")

        assert last_error is not None
        raise last_error

    def validate_config(self) -> tuple[bool, str]:
        if not self.model:
            return False, "未配置 AI 模型（AI_MODEL / model）"
        if "/" not in self.model:
            return (
                False,
                f"模型格式错误: {self.model}，应为 provider/model；"
                "使用 OpenAI-compatible 中转站时通常写 openai/实际模型名",
            )
        if not self.api_key:
            return False, "未配置 AI API Key，请在 config.yaml 或环境变量 AI_API_KEY 中设置"
        return True, ""


def build_keyword_client(ai_config: Dict[str, Any]) -> AIClient:
    """从同一份 AI 配置派生关键词客户端。

    便宜模型 = FALLBACK_MODELS 首项；未配置时回退主模型。
    """
    config = dict(ai_config or {})
    fallback = _normalize_model_list(config.get("FALLBACK_MODELS", []))
    config["MODEL"] = fallback[0] if fallback else _normalize_model(config.get("MODEL"))
    config["FALLBACK_MODELS"] = []
    config["NUM_RETRIES"] = 1
    return AIClient(config)
