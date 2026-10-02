"""One HTML document shell for already-rendered report components."""

from html import escape
from typing import Iterable

from .styles import COMMON_STYLESHEETS, load_stylesheets


def render_document(
    *,
    title: str,
    header_html: str,
    content_html: str,
    stylesheets: Iterable[str] = COMMON_STYLESHEETS,
) -> str:
    """Embed specified CSS and internal fragments without report-type branches.

    ``title`` is plain text. ``header_html`` and ``content_html`` must come from
    internal safe component/content renderers, never directly from AI, feeds or
    user input. This function does not sanitize HTML fragments.
    """
    css = load_stylesheets(stylesheets)
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(title)}</title>
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <style>
{css}  </style>
</head>
<body>
  <div class="page">
    <div class="container">
{header_html}
      <div class="report-layout">
{content_html}
      </div>
    </div>
  </div>
</body>
</html>
'''
