# coding=utf-8
"""
核心模块 - 配置管理和核心工具
"""

from trendradar.core.loader import load_config
from trendradar.core.frequency import load_frequency_words, matches_word_groups
from trendradar.core.scheduler import Scheduler, ResolvedSchedule
from trendradar.core.analyzer import (
    calculate_news_weight,
    format_time_display,
    count_word_frequency,
    count_rss_frequency,
)

__all__ = [
    "load_config",
    "load_frequency_words",
    "matches_word_groups",
    # 统计分析
    "calculate_news_weight",
    "format_time_display",
    "count_word_frequency",
    "count_rss_frequency",
    # 调度器
    "Scheduler",
    "ResolvedSchedule",
]
