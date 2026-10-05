"""RSS mode preparation and freshness conversion with explicit dependencies."""

from typing import Callable, Dict, List

from trendradar.utils.time import DEFAULT_TIMEZONE, calculate_days_old, is_within_days

from .models import RSSResult


def prepare_rss_input(
    capture: Dict,
    *,
    mode: str,
    config: Dict,
    keywords,
    convert_items: Callable,
    timezone: str,
    rank_threshold: int,
) -> RSSResult:
    """Prepare current/daily pools and interval novelty from the SAME capture."""
    from trendradar.core.analyzer import count_rss_frequency
    from trendradar.storage.base import RSSItem
    from .capture import aliases

    def convert(rows):
        grouped = {}
        for row in rows:
            grouped.setdefault(row["feed_id"], []).append(RSSItem.from_dict(row))
        converted = convert_items(grouped, capture["rss_names"])
        new_keys = set().union(*(aliases("rss", item) for item in capture["new_rss"]))
        for item in converted:
            item["is_new"] = bool(aliases("rss", item) & new_keys)
        return converted

    today = capture["captured_at"][:10]
    today_source = next((day for day in capture["sources"]["rss"] if day["date"] == today), {})
    raw_rows = today_source.get("items", [])
    if mode == "current":
        raw_rows = [item for item in raw_rows if item["last_time"] == today_source.get("latest_time")]
    new_items = convert(capture["new_rss"])
    raw_items = new_items if mode == "incremental" else convert(raw_rows)
    if not config.get("DISPLAY", {}).get("REGIONS", {}).get("RSS", True):
        return RSSResult(None, None, raw_items)

    def count(items, quiet=False):
        return count_rss_frequency(
            rss_items=items, word_groups=keywords.word_groups,
            filter_words=keywords.filter_words, global_filters=keywords.global_filters,
            new_items=new_items, max_news_per_keyword=config.get("MAX_NEWS_PER_KEYWORD", 0),
            sort_by_position_first=config.get("SORT_BY_POSITION_FIRST", False),
            timezone=timezone, rank_threshold=rank_threshold, quiet=quiet,
        )[0]

    # An empty current pool must not discard off-list interval novelty.
    stats = count(raw_items) if raw_items else []
    new_stats = count(new_items, quiet=True) if new_items and mode != "incremental" else []
    return RSSResult(stats, new_stats, raw_items)


def convert_rss_items_to_list(
    items_dict: Dict,
    id_to_name: Dict,
    *,
    rss_config: Dict,
    rss_feeds: List[Dict],
    config: Dict,
    within_days: Callable = is_within_days,
    days_old: Callable = calculate_days_old,
    log: Callable = print,
) -> List[Dict]:
    """将 RSS 条目字典转换为列表格式，并应用新鲜度过滤（用于推送）"""
    rss_items = []
    filtered_count = 0
    filtered_details = []  # 用于 DEBUG 模式下的详细日志

    # 获取新鲜度过滤配置
    freshness_config = rss_config.get("FRESHNESS_FILTER", {})
    freshness_enabled = freshness_config.get("ENABLED", True)
    default_max_age_days = freshness_config.get("MAX_AGE_DAYS", 3)
    timezone = config.get("TIMEZONE", DEFAULT_TIMEZONE)
    debug_mode = config.get("DEBUG", False)

    # 构建 feed_id -> max_age_days 的映射
    feed_max_age_map = {}
    for feed_cfg in rss_feeds:
        feed_id = feed_cfg.get("id", "")
        max_age = feed_cfg.get("max_age_days")
        if max_age is not None:
            try:
                feed_max_age_map[feed_id] = int(max_age)
            except (ValueError, TypeError):
                pass

    for feed_id, items in items_dict.items():
        # 确定此 feed 的 max_age_days
        max_days = feed_max_age_map.get(feed_id)
        if max_days is None:
            max_days = default_max_age_days

        for item in items:
            # 应用新鲜度过滤（仅在启用时）
            if freshness_enabled and max_days > 0:
                if item.published_at and not within_days(item.published_at, max_days, timezone):
                    filtered_count += 1
                    # 记录详细信息用于 DEBUG 模式
                    if debug_mode:
                        item_days_old = days_old(item.published_at, timezone)
                        feed_name = id_to_name.get(feed_id, feed_id)
                        filtered_details.append({
                            "title": item.title[:50] + "..." if len(item.title) > 50 else item.title,
                            "feed": feed_name,
                            "days_old": item_days_old,
                            "max_days": max_days,
                        })
                    continue  # 跳过超过指定天数的文章

            rss_items.append({
                "title": item.title,
                "feed_id": feed_id,
                "feed_name": id_to_name.get(feed_id, feed_id),
                "url": item.url,
                "guid": item.guid,
                "published_at": item.published_at,
                "summary": item.summary,
                "author": item.author,
            })

    # 输出过滤统计
    if filtered_count > 0:
        log(f"[RSS] 新鲜度过滤：跳过 {filtered_count} 篇超过指定天数的旧文章（仍保留在数据库中）")
        # DEBUG 模式下显示详细信息
        if debug_mode and filtered_details:
            log(f"[RSS] 被过滤的文章详情（共 {len(filtered_details)} 篇）：")
            for detail in filtered_details[:10]:  # 最多显示 10 条
                days_str = f"{detail['days_old']:.1f}" if detail['days_old'] else "未知"
                log(f"  - [{days_str}天前] [{detail['feed']}] {detail['title']} (限制: {detail['max_days']}天)")
            if len(filtered_details) > 10:
                log(f"  ... 还有 {len(filtered_details) - 10} 篇被过滤")

    return rss_items
