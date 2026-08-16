# coding=utf-8
"""
AI 分析结果格式化模块

将 AI 分析结果格式化为各推送渠道的样式
"""

import html as html_lib
import re
from .analyzer import AIAnalysisResult


def _escape_html(text: str) -> str:
    """转义 HTML 特殊字符，防止 XSS 攻击"""
    return html_lib.escape(text) if text else ""


def _inline_markdown_to_html(text: str) -> str:
    escaped = _escape_html(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def _render_markdown_fragment(
    text: str,
    *,
    number_subtitles: bool = False,
    order_bullets_when_multiple: bool = False,
) -> str:
    """宽容地把轻量 Markdown 转成安全 HTML。"""
    if not text:
        return ""
    lines = str(text).strip().splitlines()
    subtitle_count = sum(
        bool(re.match(r"^(?:###\s+.+?|【[^】]+】[:：]?)\s*$", line.strip()))
        for line in lines
    )
    bullet_count = sum(
        bool(re.match(r"^[-*]\s+\S", line.strip())) for line in lines
    )
    use_subtitle_numbers = number_subtitles and subtitle_count >= 2
    use_ordered_bullets = order_bullets_when_multiple and bullet_count >= 2
    subtitle_index = 0
    parts = ['<div class="ai-markdown">']
    list_type = None
    def close_list():
        nonlocal list_type
        if list_type:
            parts.append(f"</{list_type}>")
            list_type = None
    for raw in lines:
        line = raw.strip()
        if not line:
            close_list(); continue
        subtitle = re.match(r"^###\s+(.+?)\s*$", line)
        legacy = re.match(r"^【([^】]+)】[:：]?\s*$", line)
        if subtitle or legacy:
            close_list()
            subtitle_index += 1
            subtitle_text = (subtitle or legacy).group(1)
            if use_subtitle_numbers:
                subtitle_text = f"{subtitle_index}. {subtitle_text}"
            parts.append(f'<div class="ai-subtitle">{_inline_markdown_to_html(subtitle_text)}</div>')
            continue
        ordered = re.match(r"^\d+[.、]\s*(.+)$", line)
        if ordered:
            if list_type != "ol":
                close_list(); parts.append("<ol>"); list_type = "ol"
            parts.append(f"<li>{_inline_markdown_to_html(ordered.group(1))}</li>")
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            target_list = "ol" if use_ordered_bullets else "ul"
            if list_type != target_list:
                close_list(); parts.append(f"<{target_list}>"); list_type = target_list
            parts.append(f"<li>{_inline_markdown_to_html(bullet.group(1))}</li>")
            continue
        close_list()
        parts.append(f"<p>{_inline_markdown_to_html(line)}</p>")
    close_list(); parts.append("</div>")
    return "".join(parts)


def render_ai_analysis_html_rich(result: AIAnalysisResult) -> str:
    """渲染为丰富样式的 HTML 格式（HTML 报告用）"""
    if not result:
        return ""

    # 检查是否成功
    if not result.success:
        error_msg = result.error or "未知错误"
        return f"""
                <div class="ai-section-shell">
                <div class="ai-section">
                    <div class="ai-error">⚠️ AI 分析失败: {_escape_html(str(error_msg))}</div>
                </div>
                </div>"""

    ai_html = """
                <div class="ai-section-shell">
                <div class="ai-section">
                    <div class="ai-section-header">
                        <div class="ai-section-title">AI 新闻简报<span class="ai-intel-icon" role="img" aria-label="Apple Intelligence"></span></div>
                        <span class="ai-section-badge">AI</span>
                    </div>"""

    if result.key_news:
        content_html = _render_markdown_fragment(
            result.key_news,
            number_subtitles=True,
        )
        ai_html += f"""
                    <div class="ai-block">
                        <div class="ai-block-title">重点新闻</div>
                        <div class="ai-block-content">{content_html}</div>
                    </div>"""

    if result.brief_updates:
        content_html = _render_markdown_fragment(
            result.brief_updates,
            order_bullets_when_multiple=True,
        )
        ai_html += f"""
                    <div class="ai-block">
                        <div class="ai-block-title">简明动态</div>
                        <div class="ai-block-content">{content_html}</div>
                    </div>"""

    if result.practical_guidance:
        content_html = _render_markdown_fragment(
            result.practical_guidance,
            order_bullets_when_multiple=True,
        )
        ai_html += f"""
                    <div class="ai-block">
                        <div class="ai-block-title">实用提示</div>
                        <div class="ai-block-content">{content_html}</div>
                    </div>"""

    ai_html += """
                </div>
                </div>"""
    return ai_html
