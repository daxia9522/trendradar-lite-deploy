"""Load a fixed set of component CSS resources, independent of cwd or packaging."""

from importlib.resources import files
from typing import Iterable


COMMON_STYLESHEETS = ("base", "header", "ai")
_STYLESHEETS = frozenset((*COMMON_STYLESHEETS, "news"))


def load_stylesheets(names: Iterable[str]) -> str:
    """Inline each whitelisted resource once, preserving the requested order."""
    names = tuple(dict.fromkeys(names))
    for name in names:
        if name not in _STYLESHEETS:
            raise ValueError(f"Unknown report stylesheet: {name!r}")
    root = files("trendradar.report").joinpath("styles")
    return "\n".join(root.joinpath(f"{name}.css").read_text(encoding="utf-8") for name in names)
