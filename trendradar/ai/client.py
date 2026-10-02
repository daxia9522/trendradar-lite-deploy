# coding=utf-8
"""基于 LiteLLM 的统一 AI 客户端。"""

from __future__ import annotations

import os
import random
import time
from typing import Any, Dict, List, Optional, Sequence

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
DEFAULT_TIMEOUT = 240
DEFAULT_NUM_RETRIES = 2

# 拥塞类错误（5xx/429）是快速拒绝，用指数退避等待恢复窗口；
# 退避档数同时决定此类错误的额外重试机会（首尝试 + 档数）。
# 408 超时是确定性慢（历史 6 次同模型超时重试 0 成功），
# 单模型超时最多试 TIMEOUT_RETRY_LIMIT 次，失败立即切备用，不烧退避。
RETRY_BACKOFF_SECONDS = (5, 15, 45)
RETRY_BACKOFF_STATUSES = {429, 500, 502, 503, 504}
TIMEOUT_RETRY_LIMIT = 2


def _is_timeout_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status == 408 or type(exc).__name__ == "Timeout"


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
        if completion is None:
            raise RuntimeError(
                "LiteLLM 未安装，无法调用 AI；请安装项目锁定依赖 requirements.lock"
            )

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
            base_attempts = max(1, num_retries + 1)
            slow_delays_left = list(RETRY_BACKOFF_SECONDS)
            attempt = 0
            while True:
                attempt += 1
                timeout = params["timeout"]
                print(
                    f"[AI] 请求开始: model={model}, "
                    f"attempt={attempt}, timeout={timeout}s"
                )
                try:
                    response = completion(model=model, **params)
                    self.last_model = model
                    self.last_finish_reason = _finish_reason(response)
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
                        # 超时单模型最多 TIMEOUT_RETRY_LIMIT 次，不给慢模型第三次机会
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
