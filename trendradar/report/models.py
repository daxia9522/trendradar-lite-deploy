"""Plain-text inputs shared by the two report headers."""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ReportMeta:
    """Unescaped display strings; HTML is produced only by ``render_header``.

    The callers retain their own missing-value and timestamp policies. A frozen
    dataclass is not a sanitizer: every field is still escaped when rendered.
    """

    title: str
    meta_items: Tuple[Tuple[str, str], ...] = ()
    pills: Tuple[str, ...] = ()
