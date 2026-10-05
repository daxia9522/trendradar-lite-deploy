"""Menu-side YAML defaults, without importing the application or opening secret files."""
from __future__ import annotations

from pathlib import Path

from envfile import ConfigError
from native_schedule import ScheduleError, _strip_comment, _yaml_subset


def effective_config_path(values, app_dir: Path) -> Path:
    path = Path(values.get("CONFIG_PATH") or "config/config.yaml")
    # The shipped container mounts the host config directory under /app/config.
    if path.is_absolute() and path.is_relative_to("/app/config"):
        return Path(app_dir) / "config" / path.relative_to("/app/config")
    return path if path.is_absolute() else Path(app_dir) / path


def _lean_ai_defaults(text: str) -> dict:
    """Reuse the existing fail-closed YAML subset on just the relevant AI fields.

    Shipped [] is the empty fallback list. Full YAML syntax is delegated to
    PyYAML when available rather than inventing a second general YAML parser.
    """
    selected = []
    in_ai = False
    keep = False
    seen = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            line = _strip_comment(raw)
            if line.startswith("ai:") and line != "ai:":
                raise ScheduleError("AI flow mappings/aliases require the full YAML parser")
            in_ai = line == "ai:"
            keep = False
            if in_ai:
                if seen:
                    raise ScheduleError("duplicate ai section")
                seen = True
            continue
        if not in_ai:
            continue
        if "\t" in raw or raw.lstrip().startswith("<<:"):
            raise ScheduleError("AI tabs/merge keys require the full YAML parser")
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 2:
            key = raw.strip().partition(":")[0]
            keep = key in ("model", "api_base", "fallback_models")
            if keep and _strip_comment(raw).strip() == "fallback_models: []":
                keep = False
                selected.append("fallback_models: ''")
                continue
        if keep:
            selected.append(raw[2:])
    return _yaml_subset("\n".join(selected)) if selected else {}


def load_ai_defaults(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml
        except ImportError:
            return _lean_ai_defaults(text)
        data = yaml.safe_load(text) or {}
        ai = data.get("ai") or {}
        if not isinstance(ai, dict):
            raise ValueError
        # Menus need no YAML key value and never open AI_API_KEY_FILE.
        return {key: ai[key] for key in ("model", "api_base", "fallback_models") if key in ai}
    except (OSError, UnicodeError):
        raise ConfigError("无法读取 AI 配置默认值，未保存") from None
    except Exception:
        raise ConfigError("AI YAML 默认值无法可靠解析；请核对配置，复杂 YAML 需项目已有的 PyYAML 依赖") from None
