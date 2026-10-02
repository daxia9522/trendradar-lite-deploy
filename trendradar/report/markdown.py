"""One safe, deliberately small Markdown renderer for all report content.

Supported blocks are paragraphs (soft breaks become ``br``), h1/h2/h3,
flat lists and explicit blockquotes. Star emphasis and backtick code spans
are shared by every block. Raw HTML is escaped; links, images, tables and
fenced code are not interpreted as additional Markdown features.

Report content is rendered as supplied, without report-specific preprocessing.
"""

import html
import re


_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_ORDERED_RE = re.compile(r"^([0-9]+)[.、]\s*(\S.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(\S.*)$")
_INLINE_TOKEN_RE = re.compile(
    r"(?<!`)(?P<ticks>`+)(?!`)(?P<code>[^\n]+?)(?<!`)(?P=ticks)(?!`)"
    r"|(?P<stars>\*+)"
)
_EMPHASIS_TAGS = {1: "em", 2: "strong"}


def render_inline(text: str) -> str:
    """Escape once, then render emphasis without ever parsing inside code spans.

    Matching backtick runs delimit code. A small delimiter stack keeps nested
    star emphasis balanced; unmatched delimiters remain literal text. No input
    text can become an HTML tag name, attribute or URL.
    """
    escaped = html.escape(text)
    parts: list[str] = []
    openings: list[tuple[int, int]] = []
    cursor = 0
    for token in _INLINE_TOKEN_RE.finditer(escaped):
        parts.append(escaped[cursor:token.start()])
        cursor = token.end()
        if token.group("ticks"):
            parts.append(f'<code>{token.group("code")}</code>')
            continue
        stars = token.group("stars")
        if len(stars) > 3:
            parts.append(stars)
            continue
        can_open = token.end() < len(escaped) and not escaped[token.end()].isspace()
        can_close = token.start() > 0 and not escaped[token.start() - 1].isspace()
        remaining = len(stars)
        while can_close and openings and remaining >= openings[-1][0]:
            width, index = openings.pop()
            tag = _EMPHASIS_TAGS[width]
            parts[index] = f"<{tag}>"
            parts.append(f"</{tag}>")
            remaining -= width
        while can_open and remaining:
            width = min(2, remaining)
            openings.append((width, len(parts)))
            parts.append("*" * width)
            remaining -= width
        if remaining:
            parts.append("*" * remaining)
    parts.append(escaped[cursor:])
    return "".join(parts)


def render_markdown(text: str) -> str:
    """Render a safe semantic fragment, without a report-specific container.

    Consecutive ordinary lines share a paragraph; blank lines close blocks.
    Consecutive ``>`` lines share a blockquote. Lists are flat, accept ``-`` /
    ``*`` bullets and ``N.`` / ``N、`` markers, and preserve each ordered list's
    explicit start. Later markers in the same list follow normal HTML counting.
    """
    parts: list[str] = []
    text_lines: list[str] = []
    text_tag = "p"
    list_tag = None

    def flush_text() -> None:
        if text_lines:
            content = "<br>".join(render_inline(line) for line in text_lines)
            parts.append(f"<{text_tag}>{content}</{text_tag}>")
            text_lines.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush_text()
            close_list()
            continue
        heading = _HEADING_RE.fullmatch(line)
        if heading:
            flush_text()
            close_list()
            tag = f"h{len(heading.group(1))}"
            parts.append(f"<{tag}>{render_inline(heading.group(2))}</{tag}>")
            continue
        ordered = _ORDERED_RE.fullmatch(line)
        bullet = _BULLET_RE.fullmatch(line)
        if ordered or bullet:
            flush_text()
            target = "ol" if ordered else "ul"
            if list_tag != target:
                close_list()
                # Normalize digits without int(), including very long model output.
                start = (ordered.group(1).lstrip("0") or "0") if ordered else "1"
                start_attr = f' start="{start}"' if start != "1" else ""
                parts.append(f"<{target}{start_attr}>")
                list_tag = target
            item = ordered.group(2) if ordered else bullet.group(1)
            parts.append(f"<li>{render_inline(item)}</li>")
            continue
        close_list()
        target = "blockquote" if line.startswith(">") else "p"
        if target != text_tag:
            flush_text()
            text_tag = target
        text_lines.append(line[1:].lstrip() if target == "blockquote" else line)
    flush_text()
    close_list()
    return "".join(parts)
