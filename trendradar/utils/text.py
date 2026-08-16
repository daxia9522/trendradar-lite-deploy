# coding=utf-8
"""
共享文本规范化工具。

用于把第三方数据源中的标题、摘要等 HTML 富文本安全转换为纯文本。
"""

from html import unescape as html_unescape
from html.parser import HTMLParser
from typing import List, Optional, Tuple


class _PlainTextExtractor(HTMLParser):
    """提取常见 HTML 富文本中的可见文字。"""

    _STRIPPED_TAGS = frozenset(
        (
            "a abbr acronym address applet area article aside audio b base basefont "
            "bdi bdo big blink blockquote body br button canvas caption center cite "
            "code col colgroup command data datalist dd del details dfn dialog dir div "
            "dl dt em embed fieldset figcaption figure font footer form frame frameset "
            "h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img input ins kbd "
            "keygen label legend li link main map mark marquee math menu menuitem meta "
            "meter multicol nav nextid noframes noscript object ol optgroup option "
            "output p param picture plaintext portal pre progress q rp rt ruby s samp "
            "search section select slot small source spacer span strike strong sub "
            "summary sup svg table tbody td template textarea tfoot th thead time title "
            "tr track tt u ul var video wbr xmp animate animatemotion animatetransform "
            "circle clippath defs desc ellipse filter foreignobject g image line "
            "lineargradient marker mask metadata mpath path pattern polygon polyline "
            "radialgradient rect set stop switch symbol text textpath tspan use view "
            "annotation annotation-xml maction menclose merror mfenced mfrac mglyph mi "
            "mlabeledtr mmultiscripts mn mo mover mpadded mphantom mroot mrow ms mspace "
            "msqrt mstyle msub msubsup msup mtable mtd mtext mtr munder munderover semantics"
        ).split()
    )
    _SEPARATOR_TAGS = frozenset(
        (
            "address article aside blockquote br caption dd details dialog div dl dt "
            "fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup "
            "hr li main nav ol p pre section summary table tbody td tfoot th thead tr ul"
        ).split()
    )
    _DROP_CONTENT_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in self._DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return

        if tag in self._SEPARATOR_TAGS:
            self._parts.append(" ")
        if tag not in self._STRIPPED_TAGS:
            self._parts.append(self.get_starttag_text() or f"<{tag}>")

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        tag = tag.lower()
        if self._drop_depth or tag in self._DROP_CONTENT_TAGS:
            return

        if tag in self._SEPARATOR_TAGS:
            self._parts.append(" ")
        if tag not in self._STRIPPED_TAGS:
            self._parts.append(self.get_starttag_text() or f"<{tag} />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._DROP_CONTENT_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return

        if tag in self._SEPARATOR_TAGS:
            self._parts.append(" ")
        elif tag not in self._STRIPPED_TAGS:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._drop_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def html_to_plain_text(value: object) -> str:
    """
    将第三方富文本规范化为纯文本。

    常见 HTML 标签会被移除，标签内文字保留；HTML 实体、异常空白和
    零宽字符会被规范化。未知的尖括号标记会保留，避免误伤类似
    "C++ <vector>" 的正常标题。
    """
    raw_text = html_unescape("" if value is None else str(value)).strip()
    if not raw_text:
        return ""

    parser = _PlainTextExtractor()
    try:
        parser.feed(raw_text)
        parser.close()
        cleaned = parser.get_text()
    except Exception:
        # 极端畸形输入保留原文；最终 HTML 输出层仍会安全转义。
        cleaned = raw_text

    cleaned = cleaned.replace("\u200b", "").replace("\ufeff", "")
    return " ".join(cleaned.split())


__all__ = ["html_to_plain_text"]
