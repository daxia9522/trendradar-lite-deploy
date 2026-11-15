"""Dependency-free setup policy for grouped AI configuration.

Menus edit feature switches and named group connection variables. Model IDs,
connection references, request parameters and task policies live in config.yaml and are
validated by the application, not by a second menu-side YAML parser.
"""
from __future__ import annotations

# Keep in sync with trendradar.ai_config.LEGACY_FIELDS without importing the
# application (or PyYAML) before the native install has installed dependencies.
LEGACY_FIELDS = (
    "MODEL", "API_BASE", "API_KEY", "API_KEY_FILE", "FALLBACK_MODELS",
    "FALLBACK_API_BASE", "FALLBACK_API_KEY", "FALLBACK_API_KEY_FILE", "TIMEOUT",
    "EXTRA_PARAMS", "NUM_RETRIES",
)
RETIRED_AI_ENV_KEYS = frozenset(prefix + name
                                for prefix in ("AI_", "AI_FILTER_")
                                for name in LEGACY_FIELDS)
GROUP_KEY_FIELDS = ("GEMINI_API_KEY", "CF_API_KEY")
GROUP_KEY_FILE_FIELDS = ("GEMINI_API_KEY_FILE", "CF_API_KEY_FILE")
GROUP_ENDPOINT_FIELDS = ("CF_API_BASE",)
AI_SETUP_HELP = (
    "此菜单仅保存 AI 开关与连接组变量，不编辑模型或备用列表。"
    "请在 config/config.yaml（或 CONFIG_PATH 指定文件）的 model_groups/tasks 中维护"
    "模型、端点、model_chain、timeout、retry 和 request_params。"
    "GEMINI_API_KEY / CF_API_KEY 分别供对应连接组引用，不是逐模型密钥。"
    "CF_API_BASE 由组内 api_base_env 引用，真实地址不必写入 YAML；与 api_base 二选一。"
    "菜单不验证 YAML 或调用 AI；保存环境配置不代表模型配置已通过运行时校验。"
)
AI_FILE_HELP = (
    "原生 Linux 可选用 GEMINI_API_KEY_FILE / CF_API_KEY_FILE 保存密钥文件路径；"
    "对应组须将 api_key_env 改为 api_key_file_env 引用该变量，二者只能选择一个。"
    "菜单不会打开密钥文件；文件权限与路径由运行时安全校验。"
)
LEGACY_AI_ERROR = (
    "检测到已退役且非空的 AI 模型/连接环境字段，拒绝使用或保存。"
    "请先手工迁移到 config.yaml 的 model_groups/tasks 和 GEMINI_API_KEY/CF_API_KEY"
    "（或组级文件引用），再移除旧 AI_ / AI_FILTER_ 的模型、连接、备用和请求参数字段。"
    "不会自动转换、清空或删除旧密钥；功能开关 AI_ANALYSIS_ENABLED/AI_FILTER_ENABLED 保留。"
)


def retired_ai_errors(values) -> list[str]:
    """Match runtime's populated-field check; never return entered values."""
    return [LEGACY_AI_ERROR] if any(str(values.get(key, "")).strip()
                                    for key in RETIRED_AI_ENV_KEYS) else []
