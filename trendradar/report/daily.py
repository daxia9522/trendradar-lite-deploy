# coding=utf-8
"""Daily content renderers and document composition.

Hotlist, RSS, new items, standalone items and region order remain daily policy;
the header, AI card, Markdown and document shell are shared with weekly reports.
"""

import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .ai import render_ai_analysis_html_rich
from .components import render_header
from .helpers import html_escape, safe_report_url
from .models import ReportMeta
from .shell import render_document
from .styles import COMMON_STYLESHEETS
from trendradar.utils.time import convert_time_for_display


def _strip_html_text(value: str) -> str:
    """去掉标签与折叠图标，仅保留可见文本。"""
    text = re.sub(r'<[^>]+>', '', value or '')
    text = re.sub(r'[▼▲]\s*', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _format_section_title(title: str, count: str = '') -> str:
    """分区标题统一：本次新增热点 · 28条。"""
    name = _strip_html_text(title)
    # 去掉旧式 (共 N 条) / · N条
    name = re.sub(r'\s*[（(]共\s*\d+\s*条[)）]\s*$', '', name)
    name = re.sub(r'\s*·\s*\d+\s*条(?:热点)?\s*$', '', name).strip()
    n = ''
    if count:
        m = re.search(r'(\d+)', _strip_html_text(count))
        if m:
            n = m.group(1)
    if not n:
        m = re.search(r'(\d+)', _strip_html_text(title))
        if m:
            n = m.group(1)
    if not name:
        return title
    section_icons = {
        "本次新增热点": "🆕",
        "RSS 新增更新": "📬",
        "RSS 订阅更新": "📰",
        "独立展示区": "🔖",
    }
    icon = section_icons.get(name)
    label = f"{icon} {name}" if icon else name
    if n:
        return f'{label} · {n}条'
    return label


def _render_hotlist(report_data: Dict, *, display_mode: str) -> str:
    stats_html = ""
    if report_data["stats"]:
        total_count = len(report_data["stats"])

        for i, stat in enumerate(report_data["stats"], 1):
            count = stat["count"]

            # 确定热度等级
            if count >= 10:
                count_class = "hot"
            elif count >= 5:
                count_class = "warm"
            else:
                count_class = ""

            escaped_word = html_escape(stat["word"])

            word_count_class = f'word-count {count_class}'.rstrip()
            stats_html += f"""
                <div class="word-group inner-card">
                    <div class="word-header"><div class="word-info"><span class="word-name">{escaped_word}</span><span class="meta-sep"> · </span><span class="{word_count_class}">{count}条热点</span><span class="word-index"> ▼{i}/{total_count}</span></div></div>"""

            # 处理每个词组下的新闻标题，给每条新闻标上序号
            for j, title_data in enumerate(stat["titles"], 1):
                is_new = title_data.get("is_new", False)
                new_class = "new" if is_new else ""

                stats_html += f"""
                    <div class="news-item {new_class}">
                        <div class="news-number">{j}</div>
                        <div class="news-content">
                            <div class="news-header">"""

                # 根据 display_mode 决定显示来源还是关键词
                if display_mode == "keyword":
                    # keyword 模式：显示来源
                    stats_html += f'<span class="source-name">{html_escape(title_data["source_name"])}</span>'
                else:
                    # platform 模式：显示关键词
                    matched_keyword = title_data.get("matched_keyword", "")
                    if matched_keyword:
                        stats_html += f'<span class="keyword-tag">[{html_escape(matched_keyword)}]</span>'

                # 处理排名显示
                ranks = title_data.get("ranks", [])
                if ranks:
                    min_rank = min(ranks)
                    max_rank = max(ranks)
                    rank_threshold = title_data.get("rank_threshold", 10)

                    # 确定排名等级
                    if min_rank <= 3:
                        rank_class = "top"
                    elif min_rank <= rank_threshold:
                        rank_class = "high"
                    else:
                        rank_class = ""

                    if min_rank == max_rank:
                        rank_text = str(min_rank)
                    else:
                        rank_text = f"{min_rank}-{max_rank}"

                    stats_html += f'<span class="rank-num {rank_class}">{rank_text}</span>'

                # 处理时间显示
                time_display = title_data.get("time_display", "")
                if time_display:
                    # 简化时间显示格式，将波浪线替换为~
                    simplified_time = (
                        time_display.replace(" ~ ", "~")
                        .replace("[", "")
                        .replace("]", "")
                    )
                    stats_html += (
                        f'<span class="time-info">{html_escape(simplified_time)}</span>'
                    )

                # 处理出现次数
                count_info = title_data.get("count", 1)
                if is_new:
                    stats_html += '<span class="new-badge">NEW</span>'

                if count_info > 1:
                    stats_html += f'<span class="count-info">{count_info}次</span>'

                stats_html += """
                            </div>
                            <div class="news-title">"""

                # 处理标题和链接
                escaped_title = html_escape(title_data["title"])
                link_url = title_data.get("mobile_url") or title_data.get("url", "")

                if safe_report_url(link_url):
                    escaped_url = html_escape(link_url)
                    stats_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
                else:
                    stats_html += escaped_title

                stats_html += """
                            </div>
                        </div>
                    </div>"""

            stats_html += """
                </div>"""

    # 给热榜统计添加外层包装
    if stats_html:
        stats_html = f"""
                <div class="news-region hotlist-section">{stats_html}
                </div>"""

    return stats_html


def _render_new_items(report_data: Dict, *, show_new_section: bool) -> str:
    new_titles_html = ""
    if show_new_section and report_data["new_titles"]:
        new_section_title = _format_section_title(
            "本次新增热点", str(report_data["total_new_count"])
        )
        new_titles_html += f"""
                <div class="news-region new-section">
                    <div class="new-section-title">{new_section_title}</div>
                    <div class="new-sources-grid">"""

        for source_data in report_data["new_titles"]:
            escaped_source = html_escape(source_data["source_name"])
            titles_count = len(source_data["titles"])

            new_titles_html += f"""
                    <div class="new-source-group inner-card">
                        <div class="new-source-title">{escaped_source} · {titles_count}条</div>"""

            # 为新增新闻也添加序号
            for idx, title_data in enumerate(source_data["titles"], 1):
                ranks = title_data.get("ranks", [])

                # 处理新增新闻的排名显示
                rank_class = ""
                if ranks:
                    min_rank = min(ranks)
                    if min_rank <= 3:
                        rank_class = "top"
                    elif min_rank <= title_data.get("rank_threshold", 10):
                        rank_class = "high"

                    if len(ranks) == 1:
                        rank_text = str(ranks[0])
                    else:
                        rank_text = f"{min(ranks)}-{max(ranks)}"
                else:
                    rank_text = "?"

                new_titles_html += f"""
                        <div class="new-item">
                            <div class="new-item-number">{idx}</div>
                            <div class="new-item-rank {rank_class}">{rank_text}</div>
                            <div class="new-item-content">
                                <div class="new-item-title">"""

                # 处理新增新闻的链接
                escaped_title = html_escape(title_data["title"])
                link_url = title_data.get("mobile_url") or title_data.get("url", "")

                if safe_report_url(link_url):
                    escaped_url = html_escape(link_url)
                    new_titles_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
                else:
                    new_titles_html += escaped_title

                new_titles_html += """
                                </div>
                            </div>
                        </div>"""

            new_titles_html += """
                    </div>"""

        new_titles_html += """
                    </div>
                </div>"""

    return new_titles_html


def _render_rss_stats_html(stats: List[Dict], title: str = "RSS 订阅更新") -> str:
    """渲染 RSS 统计区块 HTML

        Args:
            stats: RSS 分组统计列表，格式与热榜一致：
                [
                    {
                        "word": "关键词",
                        "count": 5,
                        "titles": [
                            {
                                "title": "标题",
                                "source_name": "Feed 名称",
                                "time_display": "12-29 08:20",
                                "url": "...",
                                "is_new": True/False
                            }
                        ]
                    }
                ]
            title: 区块标题

        Returns:
            渲染后的 HTML 字符串
        """
    if not stats:
        return ""

    # 计算总条目数
    total_count = sum(stat.get("count", 0) for stat in stats)
    if total_count == 0:
        return ""

    rss_header = f'<div class="rss-section-title">{_format_section_title(title, str(total_count))}</div>'

    rss_html = f"""
                <div class="news-region rss-section">
                    <div class="rss-section-header">{rss_header}
                    </div>
                    <div class="rss-feeds-grid">"""

    # 按关键词分组渲染（与热榜格式一致）
    for stat in stats:
        keyword = stat.get("word", "")
        titles = stat.get("titles", [])
        if not titles:
            continue

        keyword_count = len(titles)

        feed_header = f'<span class="feed-name">{html_escape(keyword)}</span><span class="meta-sep"> · </span><span class="feed-count">{keyword_count}条</span>'
        feed_class = "feed-group inner-card"

        rss_html += f"""
                    <div class="{feed_class}">
                        <div class="feed-header">{feed_header}
                        </div>"""

        for title_data in titles:
            item_title = title_data.get("title", "")
            url = title_data.get("url", "")
            time_display = title_data.get("time_display", "")
            source_name = title_data.get("source_name", "")
            is_new = title_data.get("is_new", False)

            rss_html += """
                        <div class="rss-item">
                            <div class="rss-meta">"""

            if time_display:
                rss_html += f'<span class="rss-time">{html_escape(time_display)}</span>'

            if source_name:
                rss_html += f'<span class="rss-author">{html_escape(source_name)}</span>'

            if is_new:
                rss_html += '<span class="rss-author rss-new-marker">NEW</span>'

            rss_html += """
                            </div>
                            <div class="rss-title">"""

            escaped_title = html_escape(item_title)
            if safe_report_url(url):
                escaped_url = html_escape(url)
                rss_html += f'<a href="{escaped_url}" target="_blank" class="rss-link">{escaped_title}</a>'
            else:
                rss_html += escaped_title

            rss_html += """
                            </div>
                        </div>"""

        rss_html += """
                    </div>"""

    rss_html += """
                    </div>
                </div>"""
    return rss_html


def _render_standalone_html(
    data: Optional[Dict],
    *,
    parse_timestamp: Callable[[str], datetime] = datetime.fromisoformat,
) -> str:
    """渲染独立展示区 HTML（复用热点词汇统计区样式）

        Args:
            data: 独立展示数据，格式：
                {
                    "platforms": [
                        {
                            "id": "zhihu",
                            "name": "知乎热榜",
                            "items": [
                                {
                                    "title": "标题",
                                    "url": "链接",
                                    "rank": 1,
                                    "ranks": [1, 2, 1],
                                    "first_time": "08:00",
                                    "last_time": "12:30",
                                    "count": 3,
                                }
                            ]
                        }
                    ],
                    "rss_feeds": [
                        {
                            "id": "hacker-news",
                            "name": "Hacker News",
                            "items": [
                                {
                                    "title": "标题",
                                    "url": "链接",
                                    "published_at": "2025-01-07T08:00:00",
                                    "author": "作者",
                                }
                            ]
                        }
                    ]
                }

        Returns:
            渲染后的 HTML 字符串
        """
    if not data:
        return ""

    platforms = data.get("platforms", [])
    rss_feeds = data.get("rss_feeds", [])

    if not platforms and not rss_feeds:
        return ""

    # 计算总条目数
    total_platform_items = sum(len(p.get("items", [])) for p in platforms)
    total_rss_items = sum(len(f.get("items", [])) for f in rss_feeds)
    total_count = total_platform_items + total_rss_items

    if total_count == 0:
        return ""

    standalone_header = f'<div class="standalone-section-title">{_format_section_title("独立展示区", str(total_count))}</div>'

    standalone_html = f"""
                <div class="news-region standalone-section">
                    <div class="standalone-section-header">{standalone_header}
                    </div>"""

    standalone_html += """
                    <div class="standalone-groups-grid">"""

    # 渲染热榜平台（复用 word-group 结构）
    for platform in platforms:
        platform_name = platform.get("name", platform.get("id", ""))
        items = platform.get("items", [])
        if not items:
            continue

        group_open = '<div class="standalone-group inner-card">'
        group_header = f'<span class="standalone-name">{html_escape(platform_name)}</span><span class="meta-sep"> · </span><span class="standalone-count">{len(items)}条</span>'

        standalone_html += f"""
                    {group_open}
                        <div class="standalone-header">{group_header}
                        </div>"""

        # 渲染每个条目（复用 news-item 结构）
        for j, item in enumerate(items, 1):
            title = item.get("title", "")
            url = item.get("url", "") or item.get("mobileUrl", "")
            rank = item.get("rank", 0)
            ranks = item.get("ranks", [])
            first_time = item.get("first_time", "")
            last_time = item.get("last_time", "")
            count = item.get("count", 1)

            standalone_html += f"""
                        <div class="news-item">
                            <div class="news-number">{j}</div>
                            <div class="news-content">
                                <div class="news-header">"""

            # 排名显示（复用 rank-num 样式，无 # 前缀）
            if ranks:
                min_rank = min(ranks)
                max_rank = max(ranks)

                # 确定排名等级
                if min_rank <= 3:
                    rank_class = "top"
                elif min_rank <= 10:
                    rank_class = "high"
                else:
                    rank_class = ""

                if min_rank == max_rank:
                    rank_text = str(min_rank)
                else:
                    rank_text = f"{min_rank}-{max_rank}"

                standalone_html += f'<span class="rank-num {rank_class}">{rank_text}</span>'
            elif rank > 0:
                if rank <= 3:
                    rank_class = "top"
                elif rank <= 10:
                    rank_class = "high"
                else:
                    rank_class = ""
                standalone_html += f'<span class="rank-num {rank_class}">{rank}</span>'

            # 时间显示（复用 time-info 样式，将 HH-MM 转换为 HH:MM）
            if first_time and last_time and first_time != last_time:
                first_time_display = convert_time_for_display(first_time)
                last_time_display = convert_time_for_display(last_time)
                standalone_html += f'<span class="time-info">{html_escape(first_time_display)}~{html_escape(last_time_display)}</span>'
            elif first_time:
                first_time_display = convert_time_for_display(first_time)
                standalone_html += f'<span class="time-info">{html_escape(first_time_display)}</span>'

            # 出现次数（复用 count-info 样式）
            if count > 1:
                standalone_html += f'<span class="count-info">{count}次</span>'

            standalone_html += """
                                </div>
                                <div class="news-title">"""

            # 标题和链接（复用 news-link 样式）
            escaped_title = html_escape(title)
            if safe_report_url(url):
                escaped_url = html_escape(url)
                standalone_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
            else:
                standalone_html += escaped_title

            standalone_html += """
                                </div>
                            </div>
                        </div>"""

        standalone_html += """
                    </div>"""

    # 渲染 RSS 源（复用相同结构）
    for feed in rss_feeds:
        feed_name = feed.get("name", feed.get("id", ""))
        items = feed.get("items", [])
        if not items:
            continue

        group_open = '<div class="standalone-group inner-card">'
        group_header = f'<span class="standalone-name">{html_escape(feed_name)}</span><span class="meta-sep"> · </span><span class="standalone-count">{len(items)}条</span>'

        standalone_html += f"""
                    {group_open}
                        <div class="standalone-header">{group_header}
                        </div>"""

        for j, item in enumerate(items, 1):
            title = item.get("title", "")
            url = item.get("url", "")
            published_at = item.get("published_at", "")
            author = item.get("author", "")

            standalone_html += f"""
                        <div class="news-item">
                            <div class="news-number">{j}</div>
                            <div class="news-content">
                                <div class="news-header">"""

            # 时间显示（格式化 ISO 时间）
            if published_at:
                time_display = str(published_at)
                if "T" in time_display:
                    try:
                        time_display = parse_timestamp(time_display.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
                    except ValueError:
                        pass  # 无效日期保留原文；不吞掉程序错误或中断信号。

                standalone_html += f'<span class="time-info">{html_escape(time_display)}</span>'

            # 作者显示
            if author:
                standalone_html += f'<span class="source-name">{html_escape(author)}</span>'

            standalone_html += """
                                </div>
                                <div class="news-title">"""

            escaped_title = html_escape(title)
            if safe_report_url(url):
                escaped_url = html_escape(url)
                standalone_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
            else:
                standalone_html += escaped_title

            standalone_html += """
                                </div>
                            </div>
                        </div>"""

        standalone_html += """
                    </div>"""

    standalone_html += """
                    </div>
                </div>"""
    return standalone_html


def render_report_body(
    report_data: Dict,
    *,
    region_order: List[str],
    rss_items: Optional[List[Dict]] = None,
    rss_new_items: Optional[List[Dict]] = None,
    display_mode: str = "keyword",
    standalone_data: Optional[Dict] = None,
    ai_analysis: Optional[Any] = None,
    show_new_section: bool = True,
    parse_timestamp: Callable[[str], datetime] = datetime.fromisoformat,
) -> str:
    """Render daily sections in the requested order, retaining empty-section rules."""
    html = ""
    # 处理失败ID错误信息
    if report_data["failed_ids"]:
        html += """
                <div class="news-region error-section">
                    <div class="error-title">⚠️ 请求失败的平台</div>
                    <ul class="error-list">"""
        for id_value in report_data["failed_ids"]:
            html += f'<li class="error-item">{html_escape(id_value)}</li>'
        html += """
                    </ul>
                </div>"""

    stats_html = _render_hotlist(report_data, display_mode=display_mode)
    new_titles_html = _render_new_items(report_data, show_new_section=show_new_section)
    rss_stats_html = _render_rss_stats_html(rss_items, "RSS 订阅更新") if rss_items else ""
    # RSS new items belong to the same visibility gate as new hotlist items.
    rss_new_html = (
        _render_rss_stats_html(rss_new_items, "RSS 新增更新")
        if show_new_section and rss_new_items
        else ""
    )
    standalone_html = _render_standalone_html(standalone_data, parse_timestamp=parse_timestamp)
    ai_html = render_ai_analysis_html_rich(ai_analysis) if ai_analysis else ""

    region_contents = {
        "hotlist": stats_html,
        "rss": rss_stats_html,
        "new_items": (new_titles_html, rss_new_html),  # 元组，分别处理
        "standalone": standalone_html,
        "ai_analysis": ai_html,
    }

    # The common document layout owns spacing; components keep identical DOMs
    # regardless of their position among news or AI regions.
    for region in region_order:
        content = region_contents.get(region, "")
        if region == "new_items":
            # 特殊处理 new_items 区域（包含热榜新增和 RSS 新增两部分）
            new_html, rss_new = content
            html += new_html + rss_new
        elif content:
            html += content

    return html


def render_daily_html(
    report_data: Dict,
    mode: str = "daily",
    *,
    generated_at: str,
    region_order: Optional[List[str]] = None,
    rss_items: Optional[List[Dict]] = None,
    rss_new_items: Optional[List[Dict]] = None,
    display_mode: str = "keyword",
    standalone_data: Optional[Dict] = None,
    ai_analysis: Optional[Any] = None,
    analysis_model: str = "",
    show_new_section: bool = True,
    parse_timestamp: Callable[[str], datetime] = datetime.fromisoformat,
) -> str:
    """Render a daily document using a timestamp supplied by its caller."""
    if region_order is None:
        region_order = ["hotlist", "rss", "new_items", "standalone", "ai_analysis"]
    report_body = render_report_body(
        report_data=report_data,
        region_order=region_order,
        rss_items=rss_items,
        rss_new_items=rss_new_items,
        display_mode=display_mode,
        standalone_data=standalone_data,
        ai_analysis=ai_analysis,
        show_new_section=show_new_section,
        parse_timestamp=parse_timestamp,
    ).strip()
    if mode == "current":
        mode_label = "当前榜单"
    elif mode == "incremental":
        mode_label = "增量分析"
    else:
        mode_label = "全天汇总"
    # Match html_escape's old type boundary without escaping text twice.
    display_model = analysis_model or "未启用"
    if not isinstance(display_model, str):
        display_model = str(display_model)
    meta = ReportMeta(
        title="热点新闻分析",
        meta_items=(
            ("报告类型", mode_label),
            ("分析模型", display_model),
            ("生成时间", generated_at),
        ),
    )
    return render_document(
        title=meta.title,
        header_html=render_header(meta),
        content_html=report_body,
        stylesheets=(*COMMON_STYLESHEETS, "news"),
    )
