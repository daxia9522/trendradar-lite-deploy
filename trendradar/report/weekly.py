"""Weekly content policy and composition, independent of collection and runtime."""

import re
from typing import Dict

from .components import render_ai_card, render_header
from .markdown import render_markdown
from .models import ReportMeta
from .shell import render_document


def _render_report_content(report_markdown: str) -> str:
    """Remove duplicate weekly metadata before rendering; not a Markdown rule."""
    report_markdown = re.sub(r"^#\s+.*周报.*\n+", "", report_markdown.strip(), count=1, flags=re.MULTILINE)
    # 去掉 markdown 水平线，改由标题块承担分区，避免横线割裂
    report_markdown = re.sub(r"(?m)^(?:---+|\*\*\*+|___+)\s*$", "", report_markdown)
    # 与 Header「收集时间」重复的周期/样本元信息不进正文（模型常写成 统计/分析周期、数据样本）
    report_markdown = re.sub(
        r"(?m)^[（(]?\s*(?:\*\*)?(?:统计周期|分析周期|收集时间|时间范围|本周周期|数据样本)(?:\*\*)?\s*[:：].+$\n?",
        "",
        report_markdown,
        count=3,
    )
    report_markdown = re.sub(
        r"(?m)^[（(]\s*(?:统计周期|分析周期)\s*[:：].+[）)]\s*$\n?",
        "",
        report_markdown,
        count=1,
    )
    report_markdown = report_markdown.lstrip("\n")
    report_html = render_markdown(report_markdown)
    # HTML 层再剥首段元信息（模型有时把周期+样本塞进同一段）
    report_html = re.sub(
        r"^\s*<p>(?:(?!</p>).)*(?:统计周期|分析周期|收集时间|时间范围|数据样本)(?:(?!</p>).)*</p>\s*",
        "",
        report_html,
        count=1,
        flags=re.I | re.S,
    )
    return report_html


def render_weekly_html(
    title: str,
    date_range: str,
    model_name: str,
    statistics: Dict[str, str],
    report_markdown: str,
    *,
    generated_at: str,
) -> str:
    """Render a weekly document with an already-resolved display timestamp."""
    report_html = _render_report_content(report_markdown)
    keyword_text = statistics.get("Top5关键词") or "-"
    display_model_name = (model_name or "").strip() or "-"
    display_model_name = display_model_name[:1].upper() + display_model_name[1:]
    meta = ReportMeta(
        title=title,
        meta_items=(
            ("收集时间", date_range),
            ("周报模型", display_model_name),
            ("生成时间", generated_at),
        ),
        pills=tuple(part.strip() for part in keyword_text.split("/") if part.strip()),
    )
    return render_document(
        title=title,
        header_html=render_header(meta),
        content_html=render_ai_card(report_html),
    )
