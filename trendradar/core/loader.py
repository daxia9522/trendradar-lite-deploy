# coding=utf-8
"""
配置加载模块

负责从 YAML 配置文件和环境变量加载配置。
"""

import errno
import os
import stat
from pathlib import Path
from typing import Dict, Any, Optional

import yaml

from trendradar.ai_config import ModelRegistry, load_yaml_config
from trendradar.utils.time import DEFAULT_TIMEZONE


def _get_env_bool(key: str) -> Optional[bool]:
    """读取显式布尔值；空值沿用 YAML，拼写错误立即报错。"""
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return None
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise ValueError(f"{key} 必须为 true/false/1/0")


def _get_env_int(key: str, default: int = 0) -> int:
    """从环境变量获取整数值"""
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_env_str(key: str, default: str = "") -> str:
    """从环境变量获取字符串值"""
    return os.environ.get(key, "").strip() or default


# 密钥文件大小上限：与 deploy/docker/runtime_config.py 的 1 MiB 限制一致。
MAX_SECRET_FILE_BYTES = 1024 * 1024


def _read_secret_file(secret_path: str) -> str:
    """Read one private inode; reject symlinks at every path component.

    A configured but unsafe/unreadable file fails closed with value-free errors.
    Open directories relative to pinned descriptors so a rename cannot redirect
    the read. Sticky shared directories (e.g. /tmp) are permitted, as in the
    Docker runtime reader; the final file must be owner-only and regular.
    """
    descriptors: list[int] = []
    try:
        path = Path(secret_path)
        # Do not resolve() or normalize '..': that could hide a symlink.
        if not path.is_absolute():
            path = Path.cwd() / path
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for component in path.parts[1:-1]:
            info = os.stat(component, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("密钥文件路径不能包含符号链接")
            directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            descriptors.append(directory)
            info = os.fstat(directory)
            if stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise ValueError("密钥文件路径中的目录不能被组或其他用户写入")

        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("密钥文件必须是普通文件")
        if stat.S_IMODE(info.st_mode) & ~0o600:
            raise ValueError("密钥文件权限必须为 0600 或 0400")
        if info.st_size > MAX_SECRET_FILE_BYTES:
            raise ValueError("密钥文件超过大小上限")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(MAX_SECRET_FILE_BYTES + 1)
        if len(content) > MAX_SECRET_FILE_BYTES:
            raise ValueError("密钥文件超过大小上限")
        return content.decode("utf-8").strip()
    except UnicodeError:
        raise ValueError("密钥文件必须为 UTF-8 文本") from None
    except OSError as error:
        message = "密钥文件路径不能包含符号链接" if error.errno == errno.ELOOP else "密钥文件无法安全读取"
        raise ValueError(message) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _load_app_config(config_data: Dict) -> Dict:
    """加载应用配置"""
    app_config = config_data.get("app", {})
    advanced = config_data.get("advanced", {})
    debug_env = _get_env_bool("DEBUG")
    return {
        "TIMEZONE": _get_env_str("TIMEZONE") or _get_env_str("TZ") or app_config.get("timezone", DEFAULT_TIMEZONE),
        "DEBUG": debug_env if debug_env is not None else advanced.get("debug", False),
    }


def _load_crawler_config(config_data: Dict) -> Dict:
    """加载爬虫配置"""
    advanced = config_data.get("advanced", {})
    crawler_config = advanced.get("crawler", {})
    platforms_config = config_data.get("platforms", {})
    # 主源：环境变量优先；fallback 支持逗号分隔 env 或 yaml 列表/字符串
    api_url = _get_env_str("PLATFORMS_API_URL") or platforms_config.get("api_url", "")

    fallback_env = _get_env_str("PLATFORMS_API_FALLBACK_URLS")
    if fallback_env:
        fallback_urls = [part.strip() for part in fallback_env.split(",") if part.strip()]
    else:
        raw_fallback = platforms_config.get("api_fallback_urls", [])
        if isinstance(raw_fallback, str):
            fallback_urls = [part.strip() for part in raw_fallback.split(",") if part.strip()]
        elif isinstance(raw_fallback, list):
            fallback_urls = [str(part).strip() for part in raw_fallback if str(part).strip()]
        else:
            fallback_urls = []

    return {
        "REQUEST_INTERVAL": crawler_config.get("request_interval", 100),
        "USE_PROXY": crawler_config.get("use_proxy", False),
        "DEFAULT_PROXY": crawler_config.get("default_proxy", ""),
        "ENABLE_CRAWLER": platforms_config.get("enabled", True),
        "PLATFORMS_API_URL": api_url,
        "PLATFORMS_API_FALLBACK_URLS": fallback_urls,
    }


def _load_report_config(config_data: Dict) -> Dict:
    """加载报告配置"""
    report_config = config_data.get("report", {})

    # 环境变量覆盖
    sort_by_position_env = _get_env_bool("SORT_BY_POSITION_FIRST")
    max_news_env = _get_env_int("MAX_NEWS_PER_KEYWORD")

    return {
        "REPORT_MODE": report_config.get("mode", "daily"),
        "DISPLAY_MODE": report_config.get("display_mode", "keyword"),
        "RANK_THRESHOLD": report_config.get("rank_threshold", 10),
        "SORT_BY_POSITION_FIRST": sort_by_position_env if sort_by_position_env is not None else report_config.get("sort_by_position_first", False),
        "MAX_NEWS_PER_KEYWORD": max_news_env or report_config.get("max_news_per_keyword", 0),
    }


def _load_notification_config(config_data: Dict) -> Dict:
    """加载通知配置"""
    notification = config_data.get("notification", {})

    return {
        "ENABLE_NOTIFICATION": notification.get("enabled", True),
    }


def _load_schedule_config(config_data: Dict) -> Dict:
    """
    加载统一调度配置

    从 config.yaml 的 schedule 段读取，支持环境变量覆盖。
    """
    schedule = config_data.get("schedule", {})

    # 环境变量覆盖
    enabled_env = _get_env_bool("SCHEDULE_ENABLED")
    preset_env = _get_env_str("SCHEDULE_PRESET")

    enabled = enabled_env if enabled_env is not None else schedule.get("enabled", False)
    preset = preset_env or schedule.get("preset", "always_on")

    return {
        "enabled": enabled,
        "preset": preset,
    }


def _load_timeline_data(config_dir: str = "config") -> Dict:
    """
    加载 timeline.yaml

    Args:
        config_dir: 配置目录路径

    Returns:
        timeline.yaml 的完整数据，找不到时返回空模板
    """
    timeline_path = Path(config_dir) / "timeline.yaml"
    if not timeline_path.exists():
        print(f"[调度] timeline.yaml 未找到: {timeline_path}，使用空模板")
        return {
            "presets": {},
            "custom": {
                "default": {
                    "collect": True,
                    "analyze": False,
                    "push": False,
                    "report_mode": "current",
                    "once": {"analyze": False, "push": False},
                },
                "periods": {},
                "day_plans": {"all_day": {"periods": []}},
                "week_map": {i: "all_day" for i in range(1, 8)},
            },
        }

    with open(timeline_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    print(f"[调度] timeline.yaml 加载成功: {timeline_path}")
    return data or {}


def _load_weight_config(config_data: Dict) -> Dict:
    """加载权重配置"""
    advanced = config_data.get("advanced", {})
    weight = advanced.get("weight", {})
    return {
        "RANK_WEIGHT": weight.get("rank", 0.6),
        "FREQUENCY_WEIGHT": weight.get("frequency", 0.3),
        "HOTNESS_WEIGHT": weight.get("hotness", 0.1),
    }


def _load_rss_config(config_data: Dict) -> Dict:
    """加载 RSS 配置"""
    rss = config_data.get("rss", {})
    advanced = config_data.get("advanced", {})
    advanced_rss = advanced.get("rss", {})
    advanced_crawler = advanced.get("crawler", {})

    # RSS 代理配置：优先使用 RSS 专属代理，否则复用 crawler 的 default_proxy
    rss_proxy_url = advanced_rss.get("proxy_url", "") or advanced_crawler.get("default_proxy", "")

    # 新鲜度过滤配置
    freshness_filter = rss.get("freshness_filter", {})

    # 验证并设置 max_age_days 默认值
    raw_max_age = freshness_filter.get("max_age_days", 3)
    try:
        max_age_days = int(raw_max_age)
        if max_age_days < 0:
            print(f"[警告] RSS freshness_filter.max_age_days 为负数 ({max_age_days})，使用默认值 3")
            max_age_days = 3
    except (ValueError, TypeError):
        print(f"[警告] RSS freshness_filter.max_age_days 格式错误 ({raw_max_age})，使用默认值 3")
        max_age_days = 3

    # RSS 配置直接从 config.yaml 读取，不再支持环境变量
    return {
        "ENABLED": rss.get("enabled", False),
        "REQUEST_INTERVAL": advanced_rss.get("request_interval", 2000),
        "TIMEOUT": advanced_rss.get("timeout", 15),
        "USE_PROXY": advanced_rss.get("use_proxy", False),
        "PROXY_URL": rss_proxy_url,
        "FEEDS": rss.get("feeds", []),
        "FRESHNESS_FILTER": {
            "ENABLED": freshness_filter.get("enabled", True),  # 默认启用
            "MAX_AGE_DAYS": max_age_days,
        },
    }


def _load_display_config(config_data: Dict) -> Dict:
    """加载推送内容显示配置"""
    display = config_data.get("display", {})
    regions = display.get("regions", {})
    standalone = display.get("standalone", {})
    new_items = display.get("new_items")
    if new_items is not None:
        if not isinstance(new_items, dict):
            raise ValueError("display.new_items 必须为配置对象")
        new_items = {key.upper(): new_items.get(key, 0)
                     for key in ("max_items", "max_per_source")}
        if any(type(value) is not int or value < 0 for value in new_items.values()):
            raise ValueError("新增区限额必须为非负整数")

    # 默认区域顺序
    default_region_order = ["hotlist", "rss", "new_items", "standalone", "ai_analysis"]
    region_order = display.get("region_order", default_region_order)

    # 验证 region_order 中的值是否合法
    valid_regions = {"hotlist", "rss", "new_items", "standalone", "ai_analysis"}
    region_order = [r for r in region_order if r in valid_regions]

    # 如果过滤后为空，使用默认顺序
    if not region_order:
        region_order = default_region_order

    return {
        # 区域显示顺序
        "REGION_ORDER": region_order,
        "NEW_ITEMS": new_items,
        # 区域开关
        "REGIONS": {
            "HOTLIST": regions.get("hotlist", True),
            "NEW_ITEMS": regions.get("new_items", True),
            "RSS": regions.get("rss", True),
            "STANDALONE": regions.get("standalone", False),
            "AI_ANALYSIS": regions.get("ai_analysis", True),
        },
        # 独立展示区配置
        "STANDALONE": {
            "PLATFORMS": standalone.get("platforms", []),
            "RSS_FEEDS": standalone.get("rss_feeds", []),
            "MAX_ITEMS": standalone.get("max_items", 20),
        },
    }


def load_ai_tasks(config_path: Optional[str] = None, *, task_names=("daily_analysis",),
                  model_overrides=None) -> Dict[str, Dict]:
    """Read one registry snapshot and only the credentials used by requested tasks."""
    path = Path(config_path or os.environ.get("CONFIG_PATH", "config/config.yaml"))
    data = load_yaml_config(path.read_text(encoding="utf-8"))
    registry = ModelRegistry(data, os.environ)
    overrides = model_overrides or {}
    if set(overrides) - set(task_names):
        raise ValueError("模型覆盖引用了未请求的任务")
    return {name: registry.resolve_task(name, _read_secret_file, model_id=overrides.get(name))
            for name in task_names}


def _load_ai_analysis_config(config_data: Dict) -> Dict:
    """加载日报分析功能配置；模型及请求策略见 tasks.daily_analysis。"""
    ai_config = config_data.get("ai_analysis", {})

    enabled_env = _get_env_bool("AI_ANALYSIS_ENABLED")

    return {
        "ENABLED": enabled_env if enabled_env is not None else ai_config.get("enabled", False),
        "LANGUAGE": ai_config.get("language", "Chinese"),
        "PROMPT_FILE": ai_config.get("prompt_file", "ai_analysis_prompt.txt"),
        "INCLUDE_RANK_TIMELINE": ai_config.get("include_rank_timeline", False),
    }


def _load_ai_filter_config(config_data: Dict, config_dir: Path, registry: ModelRegistry) -> Dict:
    """加载独立筛选配置，不继承日报模型或密钥。"""
    section = config_data.get("ai_filter") or {}
    if not isinstance(section, dict):
        raise ValueError("ai_filter 必须是配置对象")
    enabled = _get_env_bool("AI_FILTER_ENABLED")
    if enabled is None:
        enabled = section.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("ai_filter.enabled 必须是布尔值")
    result = {
        "ENABLED": enabled,
        "MIN_SCORE": section.get("min_score", 0.7),
        "CONFIG_DIR": str(config_dir.resolve()),
        "CACHE_PATH": str(Path(config_data.get("storage", {}).get("local", {}).get("data_dir", "output"))
                          / "ai_filter" / "cache.sqlite3"),
        "CLIENT": {},
    }
    if enabled:
        result["CLIENT"] = registry.resolve_task("ai_filter", _read_secret_file)
    return result


def _load_storage_config(config_data: Dict) -> Dict:
    """加载存储配置"""
    storage = config_data.get("storage", {})
    formats = storage.get("formats", {})
    local = storage.get("local", {})
    remote = storage.get("remote", {})
    pull = storage.get("pull", {})

    txt_enabled_env = _get_env_bool("STORAGE_TXT_ENABLED")
    html_enabled_env = _get_env_bool("STORAGE_HTML_ENABLED")
    pull_enabled_env = _get_env_bool("PULL_ENABLED")

    return {
        "BACKEND": _get_env_str("STORAGE_BACKEND") or storage.get("backend", "auto"),
        "FORMATS": {
            "TXT": txt_enabled_env if txt_enabled_env is not None else formats.get("txt", True),
            "HTML": html_enabled_env if html_enabled_env is not None else formats.get("html", True),
        },
        "LOCAL": {
            "DATA_DIR": local.get("data_dir", "output"),
            "RETENTION_DAYS": _get_env_int("LOCAL_RETENTION_DAYS") or local.get("retention_days", 0),
        },
        "REMOTE": {
            "ENDPOINT_URL": _get_env_str("S3_ENDPOINT_URL") or remote.get("endpoint_url", ""),
            "BUCKET_NAME": _get_env_str("S3_BUCKET_NAME") or remote.get("bucket_name", ""),
            "ACCESS_KEY_ID": _get_env_str("S3_ACCESS_KEY_ID") or remote.get("access_key_id", ""),
            "SECRET_ACCESS_KEY": _get_env_str("S3_SECRET_ACCESS_KEY") or remote.get("secret_access_key", ""),
            "REGION": _get_env_str("S3_REGION") or remote.get("region", ""),
            "RETENTION_DAYS": _get_env_int("REMOTE_RETENTION_DAYS") or remote.get("retention_days", 0),
        },
        "PULL": {
            "ENABLED": pull_enabled_env if pull_enabled_env is not None else pull.get("enabled", False),
            "DAYS": _get_env_int("PULL_DAYS") or pull.get("days", 7),
        },
    }


def _load_email_config(config_data: Dict) -> Dict:
    """加载邮件通知配置。"""
    notification = config_data.get("notification", {}) or {}
    channels = notification.get("channels", {}) or {}
    email = channels.get("email", {}) or {}

    return {
        "EMAIL_FROM": _get_env_str("EMAIL_FROM") or email.get("from", ""),
        "EMAIL_PASSWORD": _get_env_str("EMAIL_PASSWORD") or email.get("password", ""),
        "EMAIL_TO": _get_env_str("EMAIL_TO") or email.get("to", ""),
        "EMAIL_SMTP_SERVER": _get_env_str("EMAIL_SMTP_SERVER") or email.get("smtp_server", ""),
        "EMAIL_SMTP_PORT": _get_env_str("EMAIL_SMTP_PORT") or email.get("smtp_port", ""),
    }


def _print_notification_sources(config: Dict) -> None:
    """打印通知渠道配置来源信息（仅邮件）。"""
    if config.get("EMAIL_FROM") and config.get("EMAIL_PASSWORD") and config.get("EMAIL_TO"):
        from_source = "环境变量" if os.environ.get("EMAIL_FROM") else "配置文件"
        print(f"已配置通知渠道: 邮件({from_source})")
    else:
        print("未配置任何通知渠道")


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载配置文件

    Args:
        config_path: 配置文件路径，默认从环境变量 CONFIG_PATH 获取或使用 config/config.yaml

    Returns:
        包含所有配置的字典

    Raises:
        FileNotFoundError: 配置文件不存在
    """
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")

    if not Path(config_path).exists():
        raise FileNotFoundError(f"配置文件 {config_path} 不存在")

    config_data = load_yaml_config(Path(config_path).read_text(encoding="utf-8"))

    print(f"配置文件加载成功: {config_path}")

    # 合并所有配置
    config = {}

    # 应用配置
    config.update(_load_app_config(config_data))

    # 爬虫配置
    config.update(_load_crawler_config(config_data))

    # 报告配置
    config.update(_load_report_config(config_data))

    # 通知配置
    config.update(_load_notification_config(config_data))

    # 统一调度配置
    config["SCHEDULE"] = _load_schedule_config(config_data)
    config["_TIMELINE_DATA"] = _load_timeline_data(
        str(Path(config_path).parent) if config_path else "config"
    )

    # 权重配置
    config["WEIGHT_CONFIG"] = _load_weight_config(config_data)

    # 平台配置
    platforms_config = config_data.get("platforms", {})
    config["PLATFORMS"] = platforms_config.get("sources", [])

    # RSS 配置
    config["RSS"] = _load_rss_config(config_data)

    # Validate one shared registry; credentials resolved once per used group.
    registry = ModelRegistry(config_data, os.environ)
    config["AI_ANALYSIS"] = _load_ai_analysis_config(config_data)
    config["AI"] = (registry.resolve_task("daily_analysis", _read_secret_file)
                    if config["AI_ANALYSIS"]["ENABLED"] else {})
    config["AI_FILTER"] = _load_ai_filter_config(config_data, Path(config_path).parent, registry)

    # 推送内容显示配置
    config["DISPLAY"] = _load_display_config(config_data)

    # 存储配置
    config["STORAGE"] = _load_storage_config(config_data)

    # 邮件通知配置
    config.update(_load_email_config(config_data))

    # 打印通知渠道配置来源
    _print_notification_sources(config)

    return config
