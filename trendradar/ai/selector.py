# coding=utf-8
"""为 AI 分析选择关键词命中的新闻，并按事件保守去重。"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class SelectedNewsCluster:
    """一个进入 AI 输入的事件簇。"""

    cluster_index: int
    representative_item: Dict[str, Any]
    member_items: List[Dict[str, Any]] = field(default_factory=list)
    group_indexes: Set[int] = field(default_factory=set)
    sources: Set[str] = field(default_factory=set)
    score: float = 0.0


@dataclass
class AIInputSelection:
    """关键词命中新闻聚簇后的选择结果。"""

    clusters: List[SelectedNewsCluster]

    @property
    def selected_count(self) -> int:
        return len(self.clusters)


def _normalize_title(value: object) -> str:
    text = str(value or "").casefold()
    # 仅移除明确的日期，保留型号、版本号和类似 15/16 的新闻事实。
    text = re.sub(r"(?:19|20)\d{2}[年/-]\d{1,2}(?:[月/-]\d{1,2}日?)?", "", text)
    text = re.sub(r"\d{1,2}月\d{1,2}日", "", text)
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _weighted_title(value: str) -> List[str]:
    """将数字按更高事实权重展开，供一次相似度计算使用。"""
    weighted: List[str] = []
    for char in value:
        weighted.extend([char] * (4 if char.isdigit() else 1))
    return weighted


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(
        None,
        _weighted_title(left),
        _weighted_title(right),
        autojunk=False,
    ).ratio()


def _same_event(left: str, right: str, threshold: float = 0.82) -> bool:
    """按一次事实加权相似度判断是否为同一事件。"""
    return _similarity(left, right) >= threshold


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


def _merge_exact(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["source"].casefold(), candidate["normalized"])
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = candidate
            continue
        existing["group_indexes"].update(candidate["group_indexes"])
        existing["keywords"].update(candidate["keywords"])
    return list(buckets.values())


def _cluster_candidates(candidates: List[Dict[str, Any]]) -> List[SelectedNewsCluster]:
    """仅与事件簇代表标题比较，避免相似标题的链式误合并。"""
    candidates = _merge_exact(candidates)
    candidates.sort(
        key=lambda candidate: (_item_score(candidate["item"]), -candidate["order"]),
        reverse=True,
    )
    clusters: List[Dict[str, Any]] = []
    for candidate in candidates:
        target = next(
            (
                cluster
                for cluster in clusters
                if _same_event(candidate["normalized"], cluster["representative"]["normalized"])
            ),
            None,
        )
        if target is None:
            clusters.append({
                "representative": candidate,
                "members": [candidate],
                "group_indexes": set(candidate["group_indexes"]),
                "sources": {candidate["source"]},
            })
            continue
        target["members"].append(candidate)
        target["group_indexes"].update(candidate["group_indexes"])
        target["sources"].add(candidate["source"])

    result: List[SelectedNewsCluster] = []
    for index, cluster in enumerate(clusters):
        members = cluster["members"]
        cross_sources = len(cluster["sources"])
        representative = cluster["representative"]
        score = max(_item_score(member["item"], cross_sources) for member in members)
        score += min(max(cross_sources - 1, 0), 5) * 8.0
        merged_items = []
        for member in members:
            merged_item = dict(member["item"])
            merged_item["_ai_source_kind"] = member["source_kind"]
            merged_item["_ai_keywords"] = sorted(member["keywords"])
            merged_items.append(merged_item)
        representative_item = dict(representative["item"])
        representative_item["_ai_source_kind"] = representative["source_kind"]
        representative_item["_ai_keywords"] = sorted(representative["keywords"])
        result.append(SelectedNewsCluster(
            cluster_index=index,
            representative_item=representative_item,
            member_items=merged_items,
            group_indexes=cluster["group_indexes"],
            sources=cluster["sources"],
            score=score,
        ))
    return result


def _cluster_sort_key(cluster: SelectedNewsCluster) -> Tuple:
    item = cluster.representative_item
    return (
        cluster.score,
        1 if item.get("is_new") else 0,
        str(item.get("last_time", item.get("time_display", ""))),
        -cluster.cluster_index,
    )


def select_ai_news(
    stats: Optional[List[Dict]],
    rss_stats: Optional[List[Dict]],
    total_limit: int,
) -> AIInputSelection:
    """将热榜与 RSS 的关键词命中统一成候选池，再按事件价值选择。"""
    total_limit = max(0, int(total_limit or 0))
    hot_group_count = len(stats or [])
    candidates = _iter_candidates(stats, "hotlist")
    candidates.extend(_iter_candidates(rss_stats, "rss", group_offset=hot_group_count))
    clusters = _cluster_candidates(candidates)
    clusters.sort(key=_cluster_sort_key, reverse=True)
    return AIInputSelection(clusters=clusters[:total_limit])


__all__ = ["AIInputSelection", "SelectedNewsCluster", "select_ai_news"]
