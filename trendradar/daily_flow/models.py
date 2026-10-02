"""Small boundaries between daily pipeline steps, not a replacement news model."""

from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional


class CrawlResult(NamedTuple):
    results: Dict
    id_to_name: Dict
    failed_ids: List


class ModeInput(NamedTuple):
    results: Dict
    id_to_name: Dict
    title_info: Dict
    new_titles: Dict


class KeywordRules(NamedTuple):
    word_groups: List[Dict]
    filter_words: List[str]
    global_filters: Optional[List[str]]


class RSSResult(NamedTuple):
    """Only RSS views consumed by downstream analysis and rendering."""

    stats: Optional[List[Dict]] = None
    new_stats: Optional[List[Dict]] = None
    raw_items: Optional[List[Dict]] = None


@dataclass(frozen=True)
class PreparedReportInput:
    mode: str
    hotlist: ModeInput
    keywords: KeywordRules
    rss: RSSResult = RSSResult()
    failed_ids: Optional[List] = None
    standalone: Optional[Dict] = None
    quiet: bool = False


class ReportArtifacts(NamedTuple):
    stats: List[Dict]
    html_file: Optional[str]
