# coding=utf-8
"""为 AI 分析选择关键词命中新闻，保守聚类后按事件评分。"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

from trendradar.ai.dataflow import is_market_dataflow_title

# 默认单来源软上限；运行时可由 ai_analysis.source_cap_ratio 覆盖。
SOURCE_CAP_RATIO = 0.30
EVENT_SIMILARITY_THRESHOLD = 0.90

_EXPLICIT_DATE_RE = re.compile(
    r"(?:19|20)\d{2}[年/-]\d{1,2}(?:[月/-]\d{1,2}日?)?|\d{1,2}月\d{1,2}日"
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
_POSITIVE_DIRECTION_RE = re.compile(r"上涨|上升|增长|增加|大涨|转盈|扭亏|回升")
_NEGATIVE_DIRECTION_RE = re.compile(r"下跌|下降|减少|大跌|转亏|亏损|回落")
_CONFLICT_TOKEN_RES = (
    re.compile(r"小组赛|淘汰赛|十六强|八强|四强|半决赛|决赛"),
    re.compile(r"红色|橙色|黄色|蓝色"),
    re.compile(r"一审|二审|再审|终审"),
)


def _normalize_source_cap_ratio(value: Any) -> float:
    """归一化单来源软上限，非法/缺失值回退默认值。"""
    if value is None:
        return SOURCE_CAP_RATIO
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return SOURCE_CAP_RATIO
    return max(0.01, min(ratio, 1.0))


@dataclass
class SelectedNewsCluster:
    """一个进入 AI 输入的事件簇。"""

    cluster_index: int
    representative_item: Dict[str, Any]
    member_items: List[Dict[str, Any]] = field(default_factory=list)
    group_indexes: Set[int] = field(default_factory=set)
    sources: Set[str] = field(default_factory=set)
    score: float = 0.0
    is_dataflow: bool = False


@dataclass
class AIInputSelection:
    """关键词候选新闻聚簇后的选择结果。"""

    clusters: List[SelectedNewsCluster]

    @property
    def selected_count(self) -> int:
        return len(self.clusters)


def _normalize_title(value: object) -> str:
    """只移除标点，保留日期、主体、方向与事实数字。"""
    return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _fact_numbers(value: object) -> Set[str]:
    """提取非年份数字；双方都有数字但完全不同时宁可不合并。"""
    numbers = set()
    for token in _NUMBER_RE.findall(str(value or "")):
        plain = token.rstrip("%")
        if len(plain) == 4 and plain.isdigit() and 1900 <= int(plain) <= 2099:
            continue
        numbers.add(token)
    return numbers


def _has_direction_conflict(left: object, right: object) -> bool:
    left_text = str(left or "")
    right_text = str(right or "")
    left_positive = bool(_POSITIVE_DIRECTION_RE.search(left_text))
    left_negative = bool(_NEGATIVE_DIRECTION_RE.search(left_text))
    right_positive = bool(_POSITIVE_DIRECTION_RE.search(right_text))
    right_negative = bool(_NEGATIVE_DIRECTION_RE.search(right_text))
    return (left_positive and right_negative) or (left_negative and right_positive)


def _has_category_conflict(left: object, right: object) -> bool:
    left_text = str(left or "")
    right_text = str(right or "")
    for pattern in _CONFLICT_TOKEN_RES:
        left_tokens = set(pattern.findall(left_text))
        right_tokens = set(pattern.findall(right_text))
        if left_tokens and right_tokens and left_tokens.isdisjoint(right_tokens):
            return True
    return False


def _has_date_conflict(left: object, right: object) -> bool:
    left_dates = set(_EXPLICIT_DATE_RE.findall(str(left or "")))
    right_dates = set(_EXPLICIT_DATE_RE.findall(str(right or "")))
    return bool(left_dates and right_dates and left_dates.isdisjoint(right_dates))


def _same_event(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """保守判断两个候选是否是同一事件。

    只接受高相似或长标题包含关系；方向冲突、事实数字完全冲突时拒绝
    合并。这样会漏掉部分改写幅度大的同一事件，但避免把不同财报、
    行情口径或伤亡进展误合并。
    """
    left_normalized = left["normalized"]
    right_normalized = right["normalized"]
    if left_normalized == right_normalized:
        return True
    if min(len(left_normalized), len(right_normalized)) < 8:
        return False
    if _has_direction_conflict(left["item"].get("title"), right["item"].get("title")):
        return False
    if _has_category_conflict(left["item"].get("title"), right["item"].get("title")):
        return False
    if _has_date_conflict(left["item"].get("title"), right["item"].get("title")):
        return False

    left_numbers = _fact_numbers(left["item"].get("title"))
    right_numbers = _fact_numbers(right["item"].get("title"))
    if left_numbers and right_numbers and left_numbers.isdisjoint(right_numbers):
        return False

    shorter, longer = sorted((left_normalized, right_normalized), key=len)
    if shorter in longer and len(shorter) / len(longer) >= 0.82:
        return True
    return (
        SequenceMatcher(None, left_normalized, right_normalized, autojunk=False).ratio()
        >= EVENT_SIMILARITY_THRESHOLD
    )


def _rank_values(item: Dict[str, Any]) -> List[int]:
    values: List[int] = []
    timeline = item.get("rank_timeline", [])
    if isinstance(timeline, list) and timeline:
        for point in timeline:
            rank = point.get("rank") if isinstance(point, dict) else point
            try:
                values.append(int(rank))
            except (TypeError, ValueError):
                values.append(0)
    if values:
        return values
    for rank in item.get("ranks", []) or []:
        try:
            values.append(int(rank))
        except (TypeError, ValueError):
            continue
    return values


def _item_score(item: Dict[str, Any], cross_sources: int = 1) -> float:
    ranks = [rank for rank in _rank_values(item) if rank > 0]
    best_rank = min(ranks) if ranks else 99
    score = max(0, 42 - min(best_rank, 42)) * 2.0
    try:
        score += min(max(int(item.get("count", 1)), 1), 10) * 3.0
    except (TypeError, ValueError):
        score += 3.0
    if item.get("is_new"):
        score += 25.0
    if len(ranks) >= 2 and max(ranks) - min(ranks) >= 5:
        score += 12.0
    score += min(max(cross_sources - 1, 0), 5) * 16.0
    return score


def _iter_candidates(
    stats: Optional[List[Dict]], source_kind: str, group_offset: int = 0
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for group_index, stat in enumerate(stats or []):
        if not isinstance(stat, dict):
            continue
        word = str(stat.get("word", "") or "").strip()
        for item in stat.get("titles", []) or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "").strip()
            if not title:
                continue
            source = str(
                item.get("source_name", item.get("feed_name", item.get("source", "未知来源")))
                or "未知来源"
            ).strip()
            normalized = _normalize_title(title)
            if not normalized:
                continue
            candidates.append({
                "item": item,
                "normalized": normalized,
                "source": source,
                "source_kind": source_kind,
                "group_indexes": {group_index + group_offset},
                "keywords": {word} if word else set(),
                "order": len(candidates),
            })
    return candidates


def _cluster_candidates(candidates: List[Dict[str, Any]]) -> List[SelectedNewsCluster]:
    """精确去重后，仅与簇代表标题做保守相似事件聚类。"""
    unique: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for candidate in candidates:
        title = str(candidate["item"].get("title", "") or "")
        if is_market_dataflow_title(title, candidate["source"]):
            continue
        key = (candidate["source"].casefold(), candidate["normalized"])
        existing = unique.get(key)
        if existing is None:
            unique[key] = dict(candidate)
            continue
        existing["group_indexes"] |= candidate["group_indexes"]
        existing["keywords"] |= candidate["keywords"]

    prepared = list(unique.values())
    prepared.sort(
        key=lambda candidate: (_item_score(candidate["item"]), -candidate["order"]),
        reverse=True,
    )

    grouped: List[Dict[str, Any]] = []
    for candidate in prepared:
        target = next(
            (
                cluster
                for cluster in grouped
                if _same_event(candidate, cluster["representative"])
            ),
            None,
        )
        if target is None:
            grouped.append({
                "representative": candidate,
                "members": [candidate],
                "group_indexes": set(candidate["group_indexes"]),
                "sources": {candidate["source"]},
            })
            continue
        target["members"].append(candidate)
        target["group_indexes"].update(candidate["group_indexes"])
        target["sources"].add(candidate["source"])

    clusters: List[SelectedNewsCluster] = []
    for index, cluster in enumerate(grouped):
        member_items = []
        for member in cluster["members"]:
            item = dict(member["item"])
            item["_ai_source_kind"] = member["source_kind"]
            item["_ai_keywords"] = sorted(member["keywords"])
            member_items.append(item)
        representative_item = member_items[0]
        clusters.append(SelectedNewsCluster(
            cluster_index=index,
            representative_item=dict(representative_item),
            member_items=member_items,
            group_indexes=set(cluster["group_indexes"]),
            sources=set(cluster["sources"]),
            # 保持当前基础评分，不恢复旧版重复叠加的跨来源奖励。
            score=max(_item_score(member["item"]) for member in cluster["members"]),
            is_dataflow=False,
        ))
    return clusters


def _cluster_sort_key(cluster: SelectedNewsCluster) -> Tuple:
    item = cluster.representative_item
    return (
        cluster.score,
        1 if item.get("is_new") else 0,
        str(item.get("last_time", item.get("time_display", ""))),
        -cluster.cluster_index,
    )


def _select_with_quota(
    clusters: List[SelectedNewsCluster],
    total_limit: int,
    source_cap_ratio: float = SOURCE_CAP_RATIO,
) -> List[SelectedNewsCluster]:
    """按事件分数与单来源软上限分配关键词候选名额。

    行情/数据播报不分配名额。分配顺序：
    1. 按 score 竞争，但受单来源软上限约束；
    2. 仍有空额时放开单来源约束补齐，避免浪费上限。
    """
    if total_limit <= 0 or not clusters:
        return []

    source_cap_ratio = _normalize_source_cap_ratio(source_cap_ratio)
    source_cap = max(1, int(total_limit * source_cap_ratio))

    # 行情流已在聚簇前排除；这里保留过滤以兼容直接构造的测试簇。
    narrative = [cluster for cluster in clusters if not cluster.is_dataflow]

    selected: List[SelectedNewsCluster] = []
    taken: Set[int] = set()
    source_used: Dict[str, int] = {}

    def primary_source(cluster: SelectedNewsCluster) -> str:
        return sorted(cluster.sources)[0] if cluster.sources else ""

    def take(cluster: SelectedNewsCluster, respect_source_cap: bool = True) -> bool:
        key = id(cluster)
        if key in taken or len(selected) >= total_limit:
            return False
        source = primary_source(cluster)
        if respect_source_cap and source_used.get(source, 0) >= source_cap:
            return False
        taken.add(key)
        selected.append(cluster)
        source_used[source] = source_used.get(source, 0) + 1
        return True

    # 1. 全局按 score 竞争，同时限制单一来源占比。
    for cluster in narrative:
        if len(selected) >= total_limit:
            break
        take(cluster)

    # 2. 补齐：来源不足时放开软上限（仍不引入行情流）。
    if len(selected) < total_limit:
        for cluster in narrative:
            if len(selected) >= total_limit:
                break
            take(cluster, respect_source_cap=False)

    selected.sort(key=_cluster_sort_key, reverse=True)
    return selected[:total_limit]


def select_ai_news(
    stats: Optional[List[Dict]],
    rss_stats: Optional[List[Dict]],
    total_limit: int,
    source_cap_ratio: float = SOURCE_CAP_RATIO,
) -> AIInputSelection:
    """将关键词命中的热榜与 RSS 聚成事件，再按新闻价值选择。"""
    total_limit = max(0, int(total_limit or 0))
    hot_group_count = len(stats or [])
    candidates = _iter_candidates(stats, "hotlist")
    candidates.extend(_iter_candidates(rss_stats, "rss", group_offset=hot_group_count))
    clusters = _cluster_candidates(candidates)
    clusters.sort(key=_cluster_sort_key, reverse=True)
    return AIInputSelection(
        clusters=_select_with_quota(
            clusters,
            total_limit,
            source_cap_ratio,
        )
    )


__all__ = [
    "AIInputSelection",
    "EVENT_SIMILARITY_THRESHOLD",
    "SelectedNewsCluster",
    "select_ai_news",
]
