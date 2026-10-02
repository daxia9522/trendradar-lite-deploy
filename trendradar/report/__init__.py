# coding=utf-8
"""Report helpers and lazily loaded generation entry points."""

from importlib import import_module

from trendradar.report.helpers import html_escape


_LAZY_EXPORTS = {
    "prepare_report_data": "trendradar.report.generator",
    "generate_html_report": "trendradar.report.generator",
}


def __getattr__(name: str):
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_LAZY_EXPORTS[name]), name)
    globals()[name] = value
    return value

__all__ = [
    # 辅助函数
    "html_escape",
    # 报告生成器
    "prepare_report_data",
    "generate_html_report",
]
