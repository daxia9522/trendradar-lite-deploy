"""Daily AI sections rendered with the shared card and Markdown presentation."""

from html import escape
from typing import Any

from .components import render_ai_card
from .markdown import render_markdown


def render_ai_analysis_html_rich(result: Any) -> str:
    """Render AISection content without importing an analyzer or model client."""
    if not result:
        return ""

    # Detailed errors remain in the caller's logs, never in the mailed document.
    if not result.success:
        return render_ai_card('<div class="ai-error">⚠️ AI 分析失败</div>')

    blocks = []
    for section in result.sections:
        content_html = render_markdown(section.content)
        title = escape(section.title) if section.title else ""
        blocks.append(f'''
              <div class="ai-block">
                <h2 class="ai-block-title">{title}</h2>
                <div class="ai-block-content">{content_html}</div>
              </div>''')

    return render_ai_card("".join(blocks), title="AI 新闻简报")
