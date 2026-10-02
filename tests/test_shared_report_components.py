"""Static shared-component CSS contracts, not a browser/client rendering test."""

import inspect
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from trendradar.report.components import render_ai_card, render_header
from trendradar.report.shell import render_document
from trendradar.report.styles import COMMON_STYLESHEETS, load_stylesheets


RESOURCE_NAMES = ("base", "header", "ai", "news")


def css_rules(css, media=()):
    """Read our controlled CSS rule blocks, retaining their media conditions.

    These resources contain no braces inside strings and no nested style rules.
    This probe deliberately does not pretend to implement a browser CSS engine.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    cursor = 0
    while css[cursor:].strip():
        opening = css.find("{", cursor)
        if opening < 0:
            raise AssertionError("CSS text outside a rule")
        selector = css[cursor:opening].strip()
        depth, closing = 1, opening + 1
        while depth and closing < len(css):
            depth += (css[closing] == "{") - (css[closing] == "}")
            closing += 1
        if depth:
            raise AssertionError("Unbalanced CSS rule")
        body = css[opening + 1:closing - 1]
        if selector.startswith("@media "):
            yield from css_rules(body, (*media, selector.removeprefix("@media ")))
        elif selector.startswith("@"):
            raise AssertionError(f"Unexpected CSS directive: {selector}")
        else:
            yield tuple(part.strip() for part in selector.split(",")), body.strip(), media
        cursor = closing


def css_declarations(body):
    """Split declarations without splitting the semicolon in an embedded URL."""
    start, quote = 0, None
    for index, char in enumerate(body):
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char and (index == 0 or body[index - 1] != "\\"):
                quote = None
        elif char == ";" and quote is None:
            declaration = body[start:index].strip()
            if declaration:
                name, value = declaration.split(":", 1)
                yield name.strip(), value.strip()
            start = index + 1
    if body[start:].strip():
        raise AssertionError("CSS declaration is missing its terminator")


def declarations_for(css, selector, *, dark=False, width=1000):
    """Resolve explicit declarations of one exact selector for static comparisons."""
    result = {}
    for selectors, body, media in css_rules(css):
        if selector not in selectors:
            continue
        if any(condition not in {"(prefers-color-scheme: dark)", "(max-width: 640px)"} for condition in media):
            raise AssertionError(f"Unknown media condition: {media}")
        if "(prefers-color-scheme: dark)" in media and not dark:
            continue
        if "(max-width: 640px)" in media and width > 640:
            continue
        for name, value in css_declarations(body):
            important = value.endswith("!important")
            if name not in result or important or not result[name][1]:
                result[name] = (value.removesuffix("!important").strip(), important)
    return {name: value for name, (value, _) in result.items()}


class SharedComponentStylesTests(unittest.TestCase):
    def test_four_resource_whitelist_validates_before_access_and_deduplicates(self):
        self.assertEqual(COMMON_STYLESHEETS, ("base", "header", "ai"))
        for bad in ("", "daily", "weekly", "daily.css", "weekly.css", "base.css", "../base", "/tmp/ai", "news?x", "other"):
            with self.subTest(bad=bad), patch("trendradar.report.styles.files") as resources:
                with self.assertRaises(ValueError):
                    load_stylesheets(("base", bad))
                resources.assert_not_called()
        self.assertEqual(load_stylesheets(("base", "header", "ai", "news", "header", "ai", "news")),
                         load_stylesheets(RESOURCE_NAMES))
        self.assertEqual(load_stylesheets(COMMON_STYLESHEETS),
                         "\n".join(load_stylesheets((name,)) for name in COMMON_STYLESHEETS))
        path = Path(__file__).resolve().parents[1] / "trendradar" / "report" / "styles"
        self.assertEqual({item.name for item in path.iterdir()}, {f"{name}.css" for name in RESOURCE_NAMES})

    def test_component_and_shell_signatures_have_no_report_type_switch(self):
        self.assertEqual(tuple(inspect.signature(render_header).parameters), ("meta",))
        self.assertEqual(tuple(inspect.signature(render_ai_card).parameters), ("body_html", "title"))
        self.assertNotIn("variant", inspect.signature(render_document).parameters)
        self.assertEqual(inspect.signature(render_document).parameters["stylesheets"].default, COMMON_STYLESHEETS)

    def test_css_ownership_and_media_rules_live_with_each_component(self):
        owners = {
            "base": r"^(?::root|body|\.page|\.container|\.report-layout)(?:$|[ :>])",
            "header": r"^\.(?:header(?:-title|-meta)?|meta-item|tab-strip|tab-pill)(?:$|[ :>])",
            "ai": r"^\.(?:ai-card(?:__[a-z]+)?|ai-intel-icon)(?:$|[ :>])",
            "news": r"^\.news-region(?:$|[ .:>])",
        }
        for name in RESOURCE_NAMES:
            with self.subTest(name=name):
                css = load_stylesheets((name,))
                rules = list(css_rules(css))
                self.assertTrue(rules)
                for selectors, body, _ in rules:
                    self.assertTrue(list(css_declarations(body)))
                    for selector in selectors:
                        self.assertRegex(selector, owners[name])
                self.assertTrue(any(not media for _, _, media in rules))
                self.assertTrue(any(media == ("(prefers-color-scheme: dark)",) for _, _, media in rules))
                self.assertTrue(any(media == ("(max-width: 640px)",) for _, _, media in rules))
                self.assertEqual(css.count("@media (prefers-color-scheme: dark)"), 1)
                self.assertEqual(css.count("@media (max-width: 640px)"), 1)
                self.assertNotRegex(css, r"\.report(?=[\s{.:>])")
                for obsolete in (".report-shell", ".ai-section-shell", ".header-meta-row", ".ai-markdown", ".ai-subtitle"):
                    self.assertNotIn(obsolete, css)

    def test_news_cannot_change_shared_header_or_ai_declarations_at_any_breakpoint(self):
        daily, weekly = load_stylesheets(RESOURCE_NAMES), load_stylesheets(COMMON_STYLESHEETS)
        self.assertTrue(daily.startswith(weekly + "\n"))
        selectors = (
            "body", ".page", ".report-layout", ".header", ".header-title", ".header-meta", ".meta-item",
            ".tab-pill", ".ai-card", ".ai-card__surface", ".ai-card__heading", ".ai-card__title", ".ai-intel-icon",
            ".ai-card__body", ".ai-card__body h1", ".ai-card__body h2", ".ai-card__body h3",
            ".ai-card__body p", ".ai-card__body li", ".ai-card__body strong", ".ai-card__body code", ".ai-card__body blockquote",
        )
        for dark in (False, True):
            for width in (1200, 641, 640, 375):
                for selector in selectors:
                    with self.subTest(dark=dark, width=width, selector=selector):
                        common = declarations_for(weekly, selector, dark=dark, width=width)
                        self.assertTrue(common)
                        self.assertEqual(declarations_for(daily, selector, dark=dark, width=width), common)

    def test_daily_visual_baseline_is_the_single_header_and_ai_design(self):
        css = load_stylesheets(COMMON_STYLESHEETS)
        header = declarations_for(css, ".header")
        self.assertEqual(header["padding"], "18px 18px 14px")
        self.assertEqual(header["border-radius"], "16px")
        self.assertEqual(header["box-shadow"], "0 8px 24px rgba(49, 134, 255, 0.18)")
        self.assertEqual(header["background-image"], "linear-gradient(to top right, #3186FF 0%, #3186FF 75%, #A9A8FF 99.6%)")
        self.assertEqual(declarations_for(css, ".header-title")["margin"], "0 0 8px")
        self.assertEqual(declarations_for(css, ".header-meta")["line-height"], "1.55")
        self.assertEqual(declarations_for(css, ".header-title", width=640)["font-size"], "17px")
        self.assertEqual(declarations_for(css, ".header-title", width=640)["margin-bottom"], "6px")
        self.assertEqual(declarations_for(css, ".header-meta", width=640)["line-height"], "1.35")
        self.assertEqual(declarations_for(css, ".meta-item", width=640)["white-space"], "normal")
        for dark in (False, True):
            for width in (1200, 640):
                surface = declarations_for(css, ".ai-card__surface", dark=dark, width=width)
                self.assertEqual(surface["background"], "#1c1c1e" if dark else "#ffffff")
                self.assertEqual(surface["padding"], "12px" if width <= 640 else "14px")
                self.assertEqual(surface["border-radius"], "13.5px")
                self.assertEqual(surface["border"], "none")
                self.assertEqual(surface["box-shadow"].count("inset"), 4)
                body = declarations_for(css, ".ai-card__body", dark=dark, width=width)
                self.assertEqual(body["font-size"], "14px")
                self.assertEqual(body["line-height"], "1.52" if width <= 640 else "1.72")
                self.assertEqual(body["color"], "#ebebf5" if dark else "#334155")
                self.assertEqual(declarations_for(css, ".header", dark=dark, width=width)["background-color"], "#3186FF")
        card = declarations_for(css, ".ai-card")
        self.assertEqual(card["border-radius"], "16px")
        self.assertEqual(card["padding"], "2.5px")
        self.assertEqual(card["background"], "linear-gradient(to bottom right, #0894ff 0%, #c959dd 34%, #ff2e54 68%, #ff9004)")
        self.assertEqual(card["box-shadow"], "0 1px 3px rgba(0, 0, 0, 0.05)")
        self.assertEqual(declarations_for(css, ".ai-card__title")["color"], "#AF52DE")
        self.assertEqual(declarations_for(css, ".ai-card__body h2")["color"], "#3186FF")
        self.assertEqual(declarations_for(css, ".ai-card__body h3")["color"], "#8B8AFF")

    def test_direct_color_fallbacks_and_embedded_icon_need_no_external_fetch(self):
        for name in RESOURCE_NAMES:
            css = load_stylesheets((name,))
            self.assertNotIn("@import", css)
            for _, body, _ in css_rules(css):
                declarations = list(css_declarations(body))
                for index, (prop, value) in enumerate(declarations):
                    if "var(" in value:
                        self.assertGreater(index, 0)
                        previous_prop, previous_value = declarations[index - 1]
                        self.assertEqual(previous_prop, prop)
                        self.assertNotIn("var(", previous_value)
                for url in re.findall(r"url\(['\"]?([^)'\"]+)", body):
                    self.assertTrue(url.startswith("data:image/"), url[:80])
        icon = declarations_for(load_stylesheets(("ai",)), ".ai-intel-icon")
        self.assertEqual(icon["mask-image"], icon["-webkit-mask-image"])
        self.assertTrue(icon["mask-image"].startswith("url('data:image/webp;base64,"))
        self.assertEqual(icon["width"], "20px")
        self.assertEqual(icon["height"], "20px")
        self.assertIn("background: #0894ff;\n  background: linear-gradient", load_stylesheets(("ai",)))
        for dark in (False, True):
            quote = declarations_for(load_stylesheets(("ai",)), ".ai-card__body blockquote", dark=dark)
            self.assertEqual(quote["background"], "#2c2c2e" if dark else "#f8fafc")
            self.assertEqual(declarations_for(load_stylesheets(("ai",)), ".ai-card__body code", dark=dark)["background"],
                             "#2c2c2e" if dark else "#f3f4f6")


if __name__ == "__main__":
    unittest.main()
