# coding=utf-8
"""TrendRadar AI 模块：日常分析 + 共用 AIClient。"""

from .analyzer import AIAnalyzer, AIAnalysisResult
from .formatter import render_ai_analysis_html_rich

__all__ = [
    "AIAnalyzer",
    "AIAnalysisResult",
    "render_ai_analysis_html_rich",
]
