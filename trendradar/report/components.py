"""Shared report components; all layout choices are independent of report type."""

from html import escape
from typing import Optional

from .models import ReportMeta


def render_header(meta: ReportMeta) -> str:
    """Render plain-text metadata and optional pills with one DOM and stylesheet."""
    rows = "\n".join(
        f'          <div class="meta-item"><strong>{escape(label)}</strong>：{escape(value)}</div>'
        for label, value in meta.meta_items
    )
    pills = ""
    if meta.pills:
        tabs = "".join(f'<span class="tab-pill">{escape(text)}</span>' for text in meta.pills)
        pills = f'\n        <div class="tab-strip">{tabs}</div>'
    return f'''      <div class="header">
        <div class="header-title">{escape(meta.title)}</div>
        <div class="header-meta">
{rows}
        </div>{pills}
      </div>'''


def render_ai_card(body_html: str, title: Optional[str] = None) -> str:
    """Wrap HTML from internal safe renderers, never raw model/feed/user text.

    The optional title is plain text. Its icon and all card markup belong here;
    callers only prepare content and decide whether a heading is needed.
    """
    heading = ""
    if title:
        heading = f'''
          <div class="ai-card__heading">
            <div class="ai-card__title">{escape(title)}<span class="ai-intel-icon" role="img" aria-label="Apple Intelligence"></span></div>
          </div>'''
    return f'''        <div class="ai-card">
          <div class="ai-card__surface">{heading}
            <div class="ai-card__body">{body_html}</div>
          </div>
        </div>'''
