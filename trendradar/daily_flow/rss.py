"""RSS mode preparation and freshness conversion with explicit dependencies."""

from typing import Callable, Dict, List

from trendradar.utils.time import DEFAULT_TIMEZONE, calculate_days_old, is_within_days

from .models import RSSResult


def prepare_rss_input(
    rss_data,
    *,
    mode: str,
    storage,
    config: Dict,
    load_frequency_words: Callable,
    convert_items: Callable,
    timezone: str,
    rank_threshold: int,
) -> RSSResult:
    """
    按报告模式处理 RSS 数据，返回与热榜相同格式的统计结构

    三种模式：
    - daily: 当日汇总，统计=当天所有条目，新增=本次新增条目
    - current: 当前榜单，统计=当前榜单条目，新增=本次新增条目
    - incremental: 增量模式，统计=新增条目，新增=无

    Args:
        rss_data: 当前抓取的 RSSData 对象

    Returns:
        RSSResult(stats, new_stats, raw_items)：
        - stats: RSS 关键词统计列表（与热榜 stats 格式一致）
        - new_stats: RSS 新增关键词统计列表（与热榜 stats 格式一致）
        - raw_items: 原始 RSS 条目列表（用于独立展示区）
        本次新增条目留在本模块内部，用于新增统计和 is_new 标记。
    """
    from trendradar.core.analyzer import count_rss_frequency

    # 从 display.regions.rss 统一控制 RSS 分析和展示
    rss_display_enabled = config.get("DISPLAY", {}).get("REGIONS", {}).get("RSS", True)

    # 加载关键词配置
    try:
        word_groups, filter_words, global_filters = load_frequency_words()
    except FileNotFoundError:
        word_groups, filter_words, global_filters = [], [], []

    max_news_per_keyword = config.get("MAX_NEWS_PER_KEYWORD", 0)
    sort_by_position_first = config.get("SORT_BY_POSITION_FIRST", False)

    rss_stats = None
    rss_new_stats = None
    raw_rss_items = None  # 原始 RSS 条目列表（用于独立展示区）
    new_items_list = None

    # 1. 首先获取原始条目（用于独立展示区，不受 display.regions.rss 影响）
    # 根据模式获取原始条目
    if mode == "incremental":
        new_items_dict = storage.detect_new_rss_items(rss_data)
        if new_items_dict:
            raw_rss_items = convert_items(new_items_dict, rss_data.id_to_name)
    elif mode == "current":
        latest_data = storage.get_latest_rss_data(rss_data.date)
        if latest_data:
            raw_rss_items = convert_items(latest_data.items, latest_data.id_to_name)
    else:  # daily
        all_data = storage.get_rss_data(rss_data.date)
        if all_data:
            raw_rss_items = convert_items(all_data.items, all_data.id_to_name)

    # 2. 获取新增条目（用于统计和 AI 输入的新增标记）
    new_items_dict = storage.detect_new_rss_items(rss_data)
    if new_items_dict:
        new_items_list = convert_items(new_items_dict, rss_data.id_to_name)
        if new_items_list:
            print(f"[RSS] 检测到 {len(new_items_list)} 条新增")

    # 如果 RSS 展示未启用，跳过关键词分析，只返回原始条目用于独立展示区
    if not rss_display_enabled:
        return RSSResult(None, None, raw_rss_items)

    # 3. 根据模式获取统计条目
    if mode == "incremental":
        # 增量模式：统计条目就是新增条目
        if not new_items_list:
            print("[RSS] 增量模式：没有新增 RSS 条目")
            return RSSResult(None, None, raw_rss_items)

        rss_stats, total = count_rss_frequency(
            rss_items=new_items_list,
            word_groups=word_groups,
            filter_words=filter_words,
            global_filters=global_filters,
            new_items=new_items_list,  # 增量模式所有都是新增
            max_news_per_keyword=max_news_per_keyword,
            sort_by_position_first=sort_by_position_first,
            timezone=timezone,
            rank_threshold=rank_threshold,
            quiet=False,
        )
        if not rss_stats:
            print("[RSS] 增量模式：关键词匹配后没有内容")
            # 即使关键词匹配为空，也返回原始条目用于独立展示区
            return RSSResult(None, None, raw_rss_items)

    else:
        mode_label = (
            "当前榜单模式" if mode == "current" else "当日汇总模式"
        )
        if not raw_rss_items:
            print(f"[RSS] {mode_label}：没有 RSS 数据")
            return RSSResult(None, None, None)

        rss_stats, total = count_rss_frequency(
            rss_items=raw_rss_items,
            word_groups=word_groups,
            filter_words=filter_words,
            global_filters=global_filters,
            new_items=new_items_list,  # 标记新增
            max_news_per_keyword=max_news_per_keyword,
            sort_by_position_first=sort_by_position_first,
            timezone=timezone,
            rank_threshold=rank_threshold,
            quiet=False,
        )
        if not rss_stats:
            print(f"[RSS] {mode_label}：关键词匹配后没有内容")
            # 即使关键词匹配为空，也返回原始条目用于独立展示区
            return RSSResult(None, None, raw_rss_items)

        # 生成新增统计
        if new_items_list:
            rss_new_stats, _ = count_rss_frequency(
                rss_items=new_items_list,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                new_items=new_items_list,
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=rank_threshold,
                quiet=True,
            )

    return RSSResult(rss_stats, rss_new_stats, raw_rss_items)


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
