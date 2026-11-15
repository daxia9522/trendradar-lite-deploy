"""日报标题归类、展示名单和分析材料。"""

from trendradar.ai.filter import LABELS
from trendradar.core.frequency import _word_matches
from trendradar.report.generator import prepare_report_data

from .market import is_routine_market_quote


def candidate_titles(hotlist, rss_result, *, mode, include_rss):
    """当前榜单与发布区间新增标题的并集。"""
    latest = None
    if mode == "current" and hotlist.title_info:
        latest = max((item.get("last_time", "") for titles in hotlist.title_info.values()
                      for item in titles.values()), default="")
    titles = []
    for source, rows in hotlist.results.items():
        for title in rows:
            if latest and hotlist.title_info.get(source, {}).get(title, {}).get("last_time") != latest:
                continue
            titles.append(title)
    for rows in hotlist.new_titles.values():
        titles.extend(rows)
    if include_rss:
        titles.extend(item.get("title", "") for item in (rss_result.raw_items or []))
        titles.extend(item.get("title", "") for item in (rss_result.raw_new_items or []))
    return list(dict.fromkeys(titles))


def keyword_groups(titles, keywords):
    """按规则顺序确定关键词栏目。"""
    groups, filters, global_filters = keywords
    decisions = dict.fromkeys(titles)
    for title in titles:
        lower = title.lower()
        if not title.strip() or any(word.lower() in lower for word in global_filters or []):
            continue
        if any(_word_matches(word, lower) for word in filters) or is_routine_market_quote(title):
            continue
        for group in groups:
            if all(_word_matches(word, lower) for word in group["required"]) and (
                not group["normal"] or any(_word_matches(word, lower) for word in group["normal"])
            ):
                decisions[title] = group["group_key"]
                break
    return decisions


def prepare_visible_report(ctx, prepared, stats):
    """生成HTML、分析和发送门控共用的展示名单。"""
    order = set(ctx.region_order)
    regions = ctx.config.get("DISPLAY", {}).get("REGIONS", {})
    show_new = ctx.show_new_section and "new_items" in order and prepared.mode != "incremental"
    visible_main = stats if "hotlist" in order and regions.get("HOTLIST", True) else []
    # 隐藏主榜不参与新增区去重。
    new_titles = {source: {title: item for title, item in rows.items() if prepared.title_groups[title]}
                  for source, rows in prepared.hotlist.new_titles.items()}
    data = prepare_report_data(
        visible_main, failed_ids=prepared.failed_ids, new_titles=new_titles,
        id_to_name=prepared.hotlist.id_to_name, mode=prepared.mode,
        rank_threshold=ctx.rank_threshold, show_new_section=show_new,
        new_items_options=ctx.config.get("DISPLAY", {}).get("NEW_ITEMS"),
        weight_config=ctx.weight_config,
    )
    has_news = any(group.get("titles") for group in data["stats"]) or bool(data["total_new_count"])
    if regions.get("RSS", True):
        if "rss" in order:
            has_news = has_news or any(group.get("titles") for group in prepared.rss.stats or [])
        if show_new:
            has_news = has_news or any(group.get("titles") for group in prepared.rss.new_stats or [])
    return data, bool(has_news)


def prepare_analysis_stats(prepared, report_data, stats):
    """按已有栏目组织展示条目，并补回采集元数据。"""
    group_names = {group["group_key"]: group.get("display_name") or group["group_key"]
                   for group in prepared.keywords.word_groups}
    grouped = {name: [] for name in LABELS}
    main_items = {(item.get("source_name", ""), item["title"]): item
                  for group in stats for item in group.get("titles", [])}

    def append(item):
        name = group_names[prepared.title_groups[item["title"]]]
        grouped.setdefault(name, []).append(item)

    for group in report_data["stats"]:
        for shown in group["titles"]:
            append(dict(main_items[(shown.get("source_name", ""), shown["title"])]))
    for source in report_data["new_titles"]:
        source_id = source["source_id"]
        raw_titles = prepared.hotlist.new_titles[source_id]
        title_info = (prepared.hotlist.title_info or {}).get(source_id, {})
        for shown in source["titles"]:
            raw = raw_titles[shown["title"]]
            metadata = title_info.get(shown["title"], {})
            item = {**metadata, **raw, **shown}
            item["count"] = raw.get("count", metadata.get("count", shown["count"]))
            append(item)
    return [{"word": name, "count": len(titles), "titles": titles}
            for name, titles in grouped.items() if titles]
