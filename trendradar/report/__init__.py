# coding=utf-8
"""
报告生成模块

提供邮件报告生成和 HTML 转义工具。

模块结构：
- helpers: HTML 转义
- html: 邮件 HTML 渲染
- generator: 报告生成器
"""

from trendradar.report.helpers import html_escape
from trendradar.report.html import render_email_html_content
from trendradar.report.generator import (
    prepare_report_data,
    generate_html_report,
)

__all__ = [
    # 辅助函数
    "html_escape",
    # HTML 渲染
    "render_email_html_content",
    # 报告生成器
    "prepare_report_data",
    "generate_html_report",
]
