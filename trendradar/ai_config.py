# coding=utf-8
"""Pure, value-safe parsing for the single positional AI configuration contract."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

# Verified against the pinned LiteLLM chat adapters. Unknown providers must
# declare an endpoint instead of inheriting an SDK-global/environment route.
DEFAULT_API_BASES = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
}


@dataclass(frozen=True)
class AICandidate:
    id: str
    model: str
    api_base: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    error: str = ""
    error_kind: str = ""
    inherits_primary: bool = False

    @property
    def provider(self) -> str:
        return self.model.partition("/")[0]

    @property
    def request_base(self) -> str:
        return self.api_base or DEFAULT_API_BASES.get(self.provider, "")


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[AICandidate, ...]
    error: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def valid_model(model: str) -> bool:
    provider, separator, actual = model.partition("/")
    return bool(separator and actual and re.fullmatch(r"[A-Za-z0-9_-]+", provider)
                and not any(char.isspace() or ord(char) < 32 for char in model))


def parse_models(value: Any) -> list[str]:
    """Preserve every declared position, including repeated model names."""
    if value is None or value == "" or isinstance(value, str) and not value.strip():
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise ValueError("AI_FALLBACK_MODELS 必须是模型列表")
    result = []
    for index, item in enumerate(items, 1):
        model = _text(item)
        if not valid_model(model):
            raise ValueError(f"AI_FALLBACK_MODELS 第 {index} 项模型格式错误，应为 provider/model")
        result.append(model)
    return result


def merge_ai_settings(ai: Mapping[str, Any], env: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the same nonblank-env-over-YAML precedence in runtime and menus.

    Reads neither the environment nor files. The runtime loader alone resolves
    AI_API_KEY_FILE; menus never open a credential file merely to validate slots.
    """
    result = {name: _text(env.get("AI_" + name)) or ai.get(name.lower(), "")
              for name in ("MODEL", "API_BASE", "API_KEY")}
    result["FALLBACK_MODELS"] = parse_models(
        _text(env.get("AI_FALLBACK_MODELS")) or ai.get("fallback_models", []))
    for name in ("FALLBACK_API_BASE", "FALLBACK_API_KEY"):
        result[name] = _text(env.get("AI_" + name))
    result["EXTRA_PARAMS"] = ai.get("extra_params") or {}
    timeout = _text(env.get("AI_TIMEOUT")) or ai.get("timeout")
    if timeout is not None and timeout != "":
        try:
            if isinstance(timeout, bool) or int(timeout) <= 0 or float(timeout) != int(timeout):
                raise ValueError
            result["TIMEOUT"] = int(timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("AI_TIMEOUT 必须是正整数") from None
    return result


def api_base_error(base: str) -> str:
    if not base:
        return ""
    try:
        parts = urlsplit(base)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.fragment or any(char.isspace() or ord(char) < 32 for char in base)):
            raise ValueError
        _ = parts.port
    except ValueError:
        return "API 地址必须是合法 HTTP(S) 地址，且不能包含用户凭据或片段"
    return ""


def endpoint_identity(candidate: AICandidate) -> tuple:
    # Default-vs-explicit remains deliberately conservative for key inheritance.
    if not candidate.api_base:
        return candidate.provider, "default"
    parts = urlsplit(candidate.api_base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (candidate.provider, parts.scheme.lower(), (parts.hostname or "").lower(),
            port, parts.path, parts.query)


def same_endpoint(first: AICandidate, second: AICandidate) -> bool:
    if api_base_error(first.api_base) or api_base_error(second.api_base):
        return False
    return endpoint_identity(first) == endpoint_identity(second)


def _candidate(ident: str, model: str, base: str, key: str, *, inherited: bool = False) -> AICandidate:
    key_field = "AI_API_KEY" if ident == "main" else f"AI_FALLBACK_API_KEY 第 {ident.split(':')[1]} 项"
    base_field = "AI_API_BASE" if ident == "main" else f"AI_FALLBACK_API_BASE 第 {ident.split(':')[1]} 项"
    if not valid_model(model):
        error, kind = "模型格式错误，应为 provider/model", "model"
    elif api_base_error(base):
        error, kind = f"{base_field}: {api_base_error(base)}", "api_base"
    elif not base and model.partition("/")[0] not in DEFAULT_API_BASES:
        error, kind = f"{base_field}: 此 provider 必须显式指定端点", "api_base"
    elif not key:
        error, kind = f"未配置 {key_field}", "api_key"
    else:
        error = kind = ""
    return AICandidate(ident, model, base, key, error, kind, inherited)


def _slots(raw: Any, count: int, name: str) -> list[str]:
    value = _text(raw)
    if not value:
        return [""] * count
    parts = [part.strip() for part in value.split("@")]
    if len(parts) != count:
        raise ValueError(f"{name} 项数 {len(parts)} 与备用模型数 {count} 不一致（空项也占位）")
    return parts


def build_candidates(config: Mapping[str, Any]) -> CandidateSet:
    """Bind one ordered list; empty columns never switch parsing semantics."""
    primary = _candidate("main", _text(config.get("MODEL")), _text(config.get("API_BASE")),
                         _text(config.get("API_KEY")))
    try:
        models = parse_models(config.get("FALLBACK_MODELS"))
        if not models and any(_text(config.get(name)) for name in ("FALLBACK_API_BASE", "FALLBACK_API_KEY")):
            raise ValueError("未配置备用模型，不能填写 AI_FALLBACK_API_BASE/KEY 列表")
        bases = _slots(config.get("FALLBACK_API_BASE"), len(models), "AI_FALLBACK_API_BASE")
        keys = _slots(config.get("FALLBACK_API_KEY"), len(models), "AI_FALLBACK_API_KEY")
    except ValueError as error:
        return CandidateSet((primary,), str(error))
    candidates = [primary]
    for index, (model, base, key) in enumerate(zip(models, bases, keys), 1):
        proposed = AICandidate(f"fallback:{index}", model, base, key)
        inherited = not key and same_endpoint(primary, proposed)
        candidates.append(_candidate(proposed.id, model, base, primary.api_key if inherited else key,
                                     inherited=inherited))
    return CandidateSet(tuple(candidates))


def validate_fallback_settings(values: Mapping[str, Any], ai_defaults: Mapping[str, Any] | None = None) -> list[str]:
    """Validate final merged slots; optional missing credentials allow draft saves."""
    try:
        resolved = build_candidates(merge_ai_settings(ai_defaults or {}, values))
    except ValueError as error:
        return [str(error)]
    if resolved.error:
        return [resolved.error]
    return [f"{candidate.id}: {candidate.error}" for candidate in resolved.candidates[1:]
            if candidate.error_kind in ("api_base", "model")]
