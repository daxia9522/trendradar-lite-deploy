# coding=utf-8
"""
HTML 报告渲染模块

提供 HTML 格式的热点新闻报告生成功能
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable

from trendradar.report.helpers import html_escape
from trendradar.utils.time import convert_time_for_display
from trendradar.ai.formatter import render_ai_analysis_html_rich


def _render_report_body(
    report_data: Dict,
    *,
    region_order: List[str],
    rss_items: Optional[List[Dict]] = None,
    rss_new_items: Optional[List[Dict]] = None,
    display_mode: str = "keyword",
    standalone_data: Optional[Dict] = None,
    ai_analysis: Optional[Any] = None,
    show_new_section: bool = True,
) -> str:
    """直接从报告数据渲染邮箱正文。"""
    html = ""
    # 处理失败ID错误信息
    if report_data["failed_ids"]:
        html += """
                <div class="error-section">
                    <div class="error-title">⚠️ 请求失败的平台</div>
                    <ul class="error-list">"""
        for id_value in report_data["failed_ids"]:
            html += f'<li class="error-item">{html_escape(id_value)}</li>'
        html += """
                    </ul>
                </div>"""

    # 生成热点词汇统计部分的HTML
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

                if link_url:
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
                <div class="hotlist-section">{stats_html}
                </div>"""

    # 生成新增新闻区域的HTML
    new_titles_html = ""
    if show_new_section and report_data["new_titles"]:
        new_section_title = _format_section_title(
            "本次新增热点", str(report_data["total_new_count"])
        )
        new_titles_html += f"""
                <div class="new-section">
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

                if link_url:
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

    # 生成 RSS 统计内容
    def render_rss_stats_html(stats: List[Dict], title: str = "RSS 订阅更新") -> str:
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
                <div class="rss-section">
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
                    rss_html += '<span class="rss-author" style="color: #dc2626;">NEW</span>'

                rss_html += """
                            </div>
                            <div class="rss-title">"""

                escaped_title = html_escape(item_title)
                if url:
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

    # 生成独立展示区内容
    def render_standalone_html(data: Optional[Dict]) -> str:
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
                <div class="standalone-section">
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
                if url:
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
                    try:
                        from datetime import datetime as dt
                        if "T" in published_at:
                            dt_obj = dt.fromisoformat(published_at.replace("Z", "+00:00"))
                            time_display = dt_obj.strftime("%m-%d %H:%M")
                        else:
                            time_display = published_at
                    except:
                        time_display = published_at

                    standalone_html += f'<span class="time-info">{html_escape(time_display)}</span>'

                # 作者显示
                if author:
                    standalone_html += f'<span class="source-name">{html_escape(author)}</span>'

                standalone_html += """
                                </div>
                                <div class="news-title">"""

                escaped_title = html_escape(title)
                if url:
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

    # 生成 RSS 统计和新增 HTML
    rss_stats_html = render_rss_stats_html(rss_items, "RSS 订阅更新") if rss_items else ""
    # RSS 新增更新属于 new_items 区域，应与 show_new_section 保持一致
    rss_new_html = (
        render_rss_stats_html(rss_new_items, "RSS 新增更新")
        if (show_new_section and rss_new_items)
        else ""
    )

    # 生成独立展示区 HTML
    standalone_html = render_standalone_html(standalone_data)

    # 生成 AI 分析 HTML
    ai_html = render_ai_analysis_html_rich(ai_analysis) if ai_analysis else ""

    # 准备各区域内容映射
    region_contents = {
        "hotlist": stats_html,
        "rss": rss_stats_html,
        "new_items": (new_titles_html, rss_new_html),  # 元组，分别处理
        "standalone": standalone_html,
        "ai_analysis": ai_html,
    }

    def add_section_divider(content: str) -> str:
        """为内容的外层 div 添加 section-divider 类"""
        if not content or 'class="' not in content:
            return content
        first_class_pos = content.find('class="')
        if first_class_pos != -1:
            insert_pos = first_class_pos + len('class="')
            return content[:insert_pos] + "section-divider " + content[insert_pos:]
        return content

    # 按 region_order 顺序组装内容，动态添加分割线
    has_previous_content = False
    for region in region_order:
        content = region_contents.get(region, "")
        if region == "new_items":
            # 特殊处理 new_items 区域（包含热榜新增和 RSS 新增两部分）
            new_html, rss_new = content
            if new_html:
                if has_previous_content:
                    new_html = add_section_divider(new_html)
                html += new_html
                has_previous_content = True
            if rss_new:
                if has_previous_content:
                    rss_new = add_section_divider(rss_new)
                html += rss_new
                has_previous_content = True
        elif content:
            if has_previous_content:
                content = add_section_divider(content)
            html += content
            has_previous_content = True


    return html

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


def render_email_html_content(
    report_data: Dict,
    mode: str = "daily",
    *,
    region_order: Optional[List[str]] = None,
    get_time_func: Optional[Callable[[], datetime]] = None,
    rss_items: Optional[List[Dict]] = None,
    rss_new_items: Optional[List[Dict]] = None,
    display_mode: str = "keyword",
    standalone_data: Optional[Dict] = None,
    ai_analysis: Optional[Any] = None,
    analysis_model: str = "",
    show_new_section: bool = True,
) -> str:
    """渲染邮箱专用 HTML。

    设计原则：
    - 外壳对齐周报双卡片（蓝 Header 卡 + 白模块卡）
    - Header「分析模型」传入本次实际调用模型（非仅配置主模型）
    - 仅 AI 有外层浅靛分析卡；新增/热榜/独立/RSS 无外层大白壳，仅组内浅卡
    - 热榜词组等可用浅内嵌卡；AI 区单卡不分内卡
    - 内容结构沿用 TrendRadar 原分区
    - 正文直接由报告数据生成，仅保留邮件可点击链接与内容区块
    """
    if region_order is None:
        region_order = ["hotlist", "rss", "new_items", "standalone", "ai_analysis"]

    report_body = _render_report_body(
        report_data=report_data,
        region_order=region_order,
        rss_items=rss_items,
        rss_new_items=rss_new_items,
        display_mode=display_mode,
        standalone_data=standalone_data,
        ai_analysis=ai_analysis,
        show_new_section=show_new_section,
    ).strip()

    if mode == "current":
        mode_label = "当前榜单"
    elif mode == "incremental":
        mode_label = "增量分析"
    else:
        mode_label = "全天汇总"

    generated_at = (get_time_func() if get_time_func else datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>热点新闻分析</title>
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
      <style>
    /* Body page colors use tokens (aligned with weekly). Other rules stay pure hex for email clients. */
    :root {{
      color-scheme: light dark;
      --page-bg: #ffffff;
      --text: #1c1c1e;
    }}
    body {{
      margin: 0;
      padding: 0;
      background: var(--page-bg);
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Arial, sans-serif;
      color: var(--text);
      line-height: 1.72;
      -webkit-font-smoothing: antialiased;
      -webkit-text-size-adjust: 100%;
    }}
    .page {{
      max-width: 920px;
      margin: 0 auto;
      padding: 0 12px 28px;
    }}
    .container {{
      background: transparent;
      box-shadow: none;
      overflow: visible;
    }}
    .header {{
      background-color: #3186FF;
      background-image: linear-gradient(
        to top right,
        #3186FF 0%,
        #3186FF 75%,
        #A9A8FF 99.6%
      );
      color: #ffffff;
      padding: 18px 18px 14px;
      text-align: left;
      border-radius: 16px;
      margin: 0 0 8px 0;
      box-shadow: 0 8px 24px rgba(49, 134, 255, 0.18);
      position: relative;
      overflow: hidden;
    }}
    .header-title {{
      position: relative;
      z-index: 1;
      margin: 0 0 8px 0;
      font-size: 22px;
      line-height: 1.25;
      font-weight: 800;
      color: #ffffff;
    }}
    .header-meta {{
      position: relative;
      z-index: 1;
      font-size: 12px;
      line-height: 1.55;
      color: rgba(255,255,255,0.92);
      display: block;
      text-align: left;
      margin: 0;
    }}
    .header-meta strong {{ color: #ffffff; }}
    .meta-item {{
      display: block;
      margin: 0 0 2px 0;
      white-space: nowrap;
    }}
    .meta-item:last-child {{ margin-bottom: 0; }}
    .content {{
      margin: 0;
      padding: 0;
      background: transparent;
    }}
    .report {{
      background: transparent;
      border: none;
      border-radius: 0;
      padding: 0;
      box-shadow: none;
      display: flex;
      flex-direction: column;
      gap: 8px;
      color: #1c1c1e;
    }}
    .module-card {{
      background: #ffffff;
      border: 1px solid rgba(60, 60, 67, 0.12);
      border-radius: 16px;
      padding: 14px;
      margin: 0 !important;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06), 0 1px 2px rgba(0, 0, 0, 0.04);
    }}
    .module-card.section-divider {{ margin-top: 0; }}
    .new-section,
    .hotlist-section,
    .standalone-section,
    .rss-section {{
      background: transparent !important;
      border: none !important;
      box-shadow: none !important;
      padding: 0 !important;
      margin: 0 !important;
    }}
    .news-link, .rss-link, .footer-link, .new-item-title a, .rss-title a, .news-title a {{
      color: #1d4ed8;
      text-decoration: none;
    }}
    .news-link:hover, .rss-link:hover, .footer-link:hover, .new-item-title a:hover, .rss-title a:hover, .news-title a:hover {{
      text-decoration: underline;
    }}
    .new-section-title, .rss-section-title, .standalone-section-title {{
      font-size: 16px;
      line-height: 1.35;
      margin: 0 12px 8px;
      color: #1e293b;
      font-weight: 700;
      letter-spacing: 0;
      padding: 0;
      background: transparent;
      border: none;
      border-radius: 0;
      display: block;
      box-sizing: border-box;
    }}
    .new-sources-grid,
    .rss-feeds-grid,
    .standalone-groups-grid {{
      margin-top: 0;
      display: block;
    }}
    .ai-section-title {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: auto;
      max-width: 100%;
      box-sizing: border-box;
      font-size: 20px;
      line-height: 1.2;
      margin: 0;
      color: #312e81;
      font-weight: 700;
      padding: 0;
      background: transparent;
      border: none;
      border-radius: 0;
    }}
    .ai-intel-icon {{
      width: 20px;
      height: 20px;
      display: inline-block;
      flex: 0 0 20px;
      position: relative;
      top: 0.5px;
      background-color: currentColor;
      -webkit-mask-image: url('data:image/webp;base64,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');
      mask-image: url('data:image/webp;base64,UklGRj4MAABXRUJQVlA4WAoAAAAQAAAATwAATwAAQUxQSNEFAAABoEVr2xnJ+qqaw+qxbdu2bdu2bdu2bdu27caY1V31XCT5U5U+5z4iJkD+l22ZGnXt3CCr7b8i9sj36AbPTPSfUDcYxZ9tI59tFCYnRrrRaMMvrFt/zqlhZCRrAeCek1BEJM54F0DDSJXmB/C7phiW/Q18SxmZjgMRVUSxhhs4GInqA4wS5XEAtSKN/1Pghq+a323giW9kaQlQREwWA+hgHd9iw1buPLZr5egWufzE/hDYKaZ3Ak99JSBfq3Hr953cu6p3fpvnog4LQfHfhc2AO7O5XAA7r4Sj+KKbv4eyPMKDHzvn8TWRusEHPHgnnUfyf8PDf84vHdq4ZsmS1Rv0n3/0Mx7+mtMDSYMBnLsHt6zbcsCisz8UPP7t1LxezWo2G33cBRCS3JTtMMDh5GJob+2tLjYxTHcU4ITNTBWA9T6ieAC4U3vS6V9mXPeWtL4AXBRFn7UA9c0cB24GiGJBgIYi4pO96bDlB0/dvHnm8JrRrQtGFZGKABUVJOAWcN5EUARQVlSPA4/s4umLwEUVKQ0QV60M8E5UqwM0FI9XBGigIs+A6mrNgAMqUV8BN+yek4vAuxgqm4B2ai2BHSozAIqJFwsBzFFZDfRUaw4cVqgCsE68uhygqsIRoKlaceCtUepQIDSRd+KHAqGpjD4CxdViA6TVi/sYoLp4uRrAw1h6GQF3DDW5AfTRcVwCmCNenw1wzqHTH7gqJscCtzXxbgCc9vee/zGASw7NXWCkmcwAhUTSPQC4G0cs6LgPcDOZSFGA9GbkArBFyn4GeJpULJn0KcD7vLIVOCemmwCuCS6AO0nEoonvAPwZ4gYamvN/i+FRh1g23jkMX/uak54Gc33FwoHrDbqLByvrONuKtW2DXTo1PdDLpdNQLN9cxz3QjO8i9FdZb6oOrPBX8t+NYUQKq8X+acDeqAr++wCcXT4AM602FPjZ5S/AcX+j1QBhxWQQ8MNhrSjBwDQpFAywxa7XE+BtZpFYP4Fu1moBOJOKpH0D0F8n0z8gJK2IyBzgqrVOA6tERFK8A/5l1RwF/uUTbTaAzFZKB5BfIzl+AztFpABAL9G/CYyw0gDgoeh3A8gkMg2462PQHzhnpZPAMAPfJ8AYkfNAJzHMDkQ4rBMjHMhjIH2AkyJhQDYj2yegtHVKACF2o5xAsMhXIKWR7AJ6WacnsEeMUwJfRd4BeRTGACutswoYq5AT+CRyCuik0Ag4ap3jQEOFDsAZkcnAWYUSwAPrPASKKpwGJosUBqhrlAEItc43IK1RVYDCInIR+FbYIDHwxzoAQQZ5woC7NhEp4Qac/Xx1ggAsE6iJquPX5zdAFdFOQPuoY3TLBWlERGJ2fYp2gejaFmvg17ZWySJDyra7/6C7yU9PpONfjfal9V5h+HeQTRQz7DLQvTanYXzvRS836qTGeGt6MZlj4TcF7Z1xebyRbfTFCJQ/z8ksHvSvOPeBCvBqaHzPxBvyEPW7M8v7iccTNlKCfyvTmku7/C/q9eKId4M0LZc+1wPn3ARqCeaFYxiyo5cmSKxgE0nV7YxbA2HNFGytvqB/sU9Wm4gl7Jog0SYdE6yBffH1Eh9GN2R8CtEGaKJ6S5xAIh2RwF6hGt4W1pQPRhvaM1D04wMu8fo7IJeBiGOGC8DZTewj3QARU2KKcSbgk/cuA7UVRAo+RLt2L9rbeUW1CnDbe6uB0UoSbZ1Gf2GgKA8AtnivD3BSTaSLU+9nIzF5GBjqvbyAK4EJma83V0wmcQIFvWd/C0xWi7ERw60OtdnAB7v3ZALgLKFS7CmKL0uplHMBQ8SCiX4Av5saJF7mBgjv0uYf2nUpDJr+AkJiW0G6oD3XOIk9ca01f9G+LiSS/4WGiI1VgyRBo7Nom4o1F2rUt8QWEYm1QaM+RSxqG2fifT0xrPlazT3EZhWRYjcUQoZHE8UoAz4oXC0iVraVWfbMjfv5yoYBYtK/4ZaPwIsVZWxieZ8gP/FwtARR5X8UAFZQOCBGBgAAsCIAnQEqUABQAD5pKJFFpCKhmFnfqEAGhLSACvGf5T+IHfh/G+ij60euP4oe8H/AbRT6KfbPyy/Jb3z/tfABejf6v+Tv5F6IF63fJP71/Iv6r/1dZT/Mf8l/MvS7+h/p35JnwD+9/pX8AH8G/oP+p9jT8f/tv6j/9L+4ewv8U/jf+i/sHwBfxj+U/5z+2fvV/qP/////sz9Rv6Hewb+nP37rfqxK59naoWwWXlQN2A41XH/PEXPzJa8qTVCHt2Ae/B18+8Rpq/9W1XtE70Ek4dUYdFZ5VJ3byPvwLtrCm6bw6rU1EqzTqkD8+jsVsfnZhPssRwKsDwxwV70+44n/jI6EV1aZREzdkumC314YKAvE3jBCqxfNtgYSNgwYAAD+SlT+cgSz6vlGgcmEysBvKroiwdV3rPNNu/pyvT9Sd1k/JmrAOd2ZinZHd/iCTlpuhSlAnZil8QyEsq+rGlJcAoZ/lTNYYGLP/6WMF+aPa4XTXkWxfUWccrhuB70Hu7CIjOJk1tsrdqnMs/sbt/jUirxsyfxeHwXByf+6p6uu9sZaIMj8UGKuy+EQ8WhO7XnhZMWorRxLfFS4De9Xn3xyLznuy+WRZHxt+Q71Thi7UT/yeD0e8x5bV1dT12sRZvABWsPZrbbYlLkh41Z/IkSyMrInR6s98QAYxUR2K+RNyCR5LivEq4ukOPY8iyWM37nbJ+7gLN6BN5PoEGEY8r9nEXETVzdbCBkLeOMm8N9PaQD++8acf/k0U7fIfo55AABQPUPZ/0+MS8FgzPvLo+jeij8mOKtW5jpxSDMoRkN97IKZo//5r2W0M5ZN2g6vM7oGtqs7LXg2C6TAYHU93nyav7hAlXRh3Jd82ySw/bhFG4iMuQb4UfMbYZvf2CBrwVrvFFz8HFhoCO+RcdZOOuykZp90Jyu0zxnZRqj/CBJpQJIvTgPyQ9tmAPCnjgohqNvI2nijdGOGOkt8Rq2TXGRxGrjpra/r7GO73lI4sYzUI9/j8PJuKb24r1TNSOZngHN/gCzYqwlCM/HDRqR/+TXXRJhc5j4b4Q+7En5iO84dRz/wL8zTtD11AufSPhYbPnbQ+v4VLaAqa7yRhU8//91Ul9dpMS0QEWo0hY945/hzQCNlrFn6J8SQ/gFZaeOUcr/b4wD/n9jp3U1j8lXdobc44EnNR/pQbl3YMySvajP6DgwJEFx5eyJnk8Y1uWMb7gL5iu2hXqPU4cdDwpNdcefuOyOGSyf9Tv5vX/on8hp1pFeYnsNgL2UtDiMY8Ebk0U6hldJ1P1r6Ir1M9yEuD4Tq5iR//gSq/he6tIwqE/uYLjoFgSTmvLrzVS9gcDk58N/3Cw1esv8YTBVrmiJ2AND8Bel6YTjO+6RUDIWn9sBR4jBjiYv0Yif8UeILT6gg4z2MmeCuBLt5tSpQ8ZkkkzssiT9/BjsczfeKxde5Nv34rbY3E94nUTn0Aq75lwLMfrw1qGIIfJd7pfOLajFVcI/9e/5GwK7cjsNh+8qmJ9WJ5/bm3W+CbFIjP+aF0VpKeZyC5CKNxXIQ1dBsvsEy7drrAwiyEAf5t01mo+3dCHvc8xHeCcHqGAM12Gr2OLGSAH3g493gfgDIQB8j3QH9f6yy2B3fKZXpDJ9DGN6YOTlH3YEZV5hmsn28IBjTVH/x0fK3iS3RQWSOO+oOvEeqaShCfGEM5LF9FbcJMoHglkXaWwJZYRRPRIywe2K8PBixZS+w2Fx4a+86q3w5tAdguu77SAgpWT9w6YDFckEQx87LYpfwM/PVoa7SL5UY9KLkuuSVTUuPC/tIkshvXHyWeq9jU8IkXclzGUtGQaO7oXMv8ExhpE0diZ1oQX1ibf7wOB8a70is6fQe1nIyGkhrns9m33oqXtlbs0MZ8oV+AJgMu56yLTCYNeVNiDe5qNltZS2fcXg/Xm+YyqWZguz/Uziio/kn+8sHr4cHTHM2bW92oGeYQIFcx9LWP2vGjs0z2Hyg+OpFFjPz35DWOL4fr789pTI3+i5lDC499GwV/PS9KzFf6ML5NlZf4C9W6ngPtbL/+nAJD57KBFbMtjk8Ezl1vEkvv1fxgzgE0clHv71UcYDDqN7nDZri2zDpQbb55hH/69req6SIAjVcxfBlD10+5V/xCQAAAA==');
      -webkit-mask-repeat: no-repeat;
      mask-repeat: no-repeat;
      -webkit-mask-position: center;
      mask-position: center;
      -webkit-mask-size: contain;
      mask-size: contain;
    }}
    .rss-section-header, .standalone-section-header {{
      display: block;
      width: 100%;
      margin: 0 0 12px 0;
    }}
    .ai-section-header {{
      display: block;
      width: 100%;
      margin: 0 0 14px 0;
    }}
    .meta-title {{ font-weight: 700; color: inherit; }}
    .meta-sep {{
      color: #94a3b8;
      font-weight: 500;
      margin: 0 1px;
    }}
    .rss-section-count, .standalone-section-count, .rss-count, .feed-count, .new-source-title, .word-count, .word-index, .standalone-count {{
      color: #6b7280;
      font-size: 13px;
      font-weight: 500;
    }}
    .inner-card {{
      background: #ffffff;
      border: 1px solid rgba(60, 60, 67, 0.12);
      border-radius: 16px;
      padding: 10px 12px 8px;
      margin: 0 0 8px 0;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06), 0 1px 2px rgba(0, 0, 0, 0.04);
      overflow: hidden;
    }}
    .inner-card:last-child {{ margin-bottom: 0; }}
    .word-group {{ margin-bottom: 8px; }}
    .word-header, .feed-header, .standalone-header {{
      display: flex;
      justify-content: flex-start;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;
      padding-bottom: 6px;
      border-bottom: 0.75px solid rgba(60, 60, 67, 0.12);
      background: transparent;
    }}
    .word-info {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
    }}
    .word-name, .feed-name, .standalone-name {{
      font-size: 15px;
      font-weight: 700;
      color: #374151;
    }}
    .word-index {{
      color: #94a3b8;
      font-size: 12px;
      font-weight: 500;
      margin-left: 4px;
    }}
    .collapse-icon {{ display: none !important; }}
    .news-item, .rss-item, .new-item {{
      padding: 6px 0;
      border-bottom: 0.75px solid rgba(60, 60, 67, 0.12);
    }}
    .news-item:last-child, .rss-item:last-child, .new-item:last-child {{ border-bottom: none; }}
    .news-item {{ display: flex; gap: 11px; align-items: center; }}
    .news-number, .new-item-number {{
      flex-shrink: 0;
      width: 20px;
      height: 20px;
      border-radius: 999px;
      background: #f3f4f6;
      color: #6b7280;
      font-size: 12px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-align: center;
      line-height: 20px;
      vertical-align: middle;
    }}
    .news-content, .new-item-content {{ flex: 1; min-width: 0; }}
    .news-header, .rss-meta {{
      margin-bottom: 3px;
      font-size: 12px;
      color: #6b7280;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 4px;
    }}
    .news-title, .new-item-title, .rss-title {{
      font-size: 15px;
      line-height: 1.48;
      color: #111827;
    }}
    .new-source-group {{ margin-bottom: 8px; }}
    .new-source-title {{
      margin: 0 0 4px 0;
      color: #4b5563;
    }}
    .new-item {{
      display: grid;
      grid-template-columns: 20px 30px 1fr;
      column-gap: 7px;
      align-items: center;
    }}
    .new-item-rank, .rank-num {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 24px;
      padding: 0 3px;
      border-radius: 999px;
      background: #f3f4f6;
      color: #6b7280;
      font-size: 11px;
      font-weight: 700;
      text-align: center;
      margin-right: 0;
      line-height: 16px;
      height: 16px;
      vertical-align: middle;
    }}
    .new-item-rank.top, .rank-num.top {{
      background: #fee2e2;
      color: #dc2626;
    }}
    .new-item-rank.high, .rank-num.high {{
      background: #ffedd5;
      color: #ea580c;
    }}
    .time-info, .count-info, .source-name, .rss-time, .rss-author {{
      margin-right: 4px;
      color: #6b7280;
      font-size: 12px;
    }}
    .source-name {{
      font-size: 13px;
      font-weight: 600;
      color: #4b5563;
    }}
    .new-badge {{
      display: inline-block;
      margin-right: 6px;
      padding: 0 6px;
      height: 16px;
      line-height: 16px;
      border-radius: 999px;
      background: #fbbf24;
      color: #7c2d12;
      font-size: 10px;
      font-weight: 700;
      vertical-align: middle;
      box-shadow: inset 0 0 0 1px rgba(146, 64, 14, 0.10);
    }}
    .rss-item {{
      background: #f0fdf4;
      border-left: 3px solid #22c55e;
      border-radius: 16px;
      padding: 10px 12px;
      margin-bottom: 8px;
    }}
    .rss-item:last-child {{ margin-bottom: 0; }}
    .rss-author {{
      color: #166534;
      font-weight: 500;
    }}
    .ai-section-badge {{ display: none; }}
    /* AI 卡：外层渐变描边 + 内层白底内晕；邮件兼容，不使用 mask/filter */
    .ai-section-shell {{
      border-radius: 16px;
      padding: 2.5px;
      background: linear-gradient(to bottom right, #0894ff 0%, #c959dd 34%, #ff2e54 68%, #ff9004);
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    }}
    .ai-section {{
      position: relative;
      overflow: hidden;
      background: #ffffff;
      border: none;
      border-radius: 13.5px;
      padding: 14px;
      color: #374151;
      box-shadow:
        inset 0 0 10px rgba(8, 148, 255, 0.14),
        inset 0 0 14px rgba(201, 89, 221, 0.11),
        inset 0 0 18px rgba(255, 46, 84, 0.09),
        inset 0 0 22px rgba(255, 144, 4, 0.08);
    }}
    .ai-block {{
      margin: 0;
      padding: 0;
      background: transparent;
      border: none;
      border-radius: 0;
      box-shadow: none;
    }}
    .ai-block + .ai-block {{
      margin-top: 12px;
      padding-top: 12px;
      border-top: 0.75px solid rgba(60, 60, 67, 0.12);
    }}
    /* AI hierarchy: section title > block title > subtitle > body */
    .ai-block-title {{
      font-size: 15px;
      font-weight: 700;
      color: #3730a3;
      margin: 0 0 8px 0;
    }}
    .ai-block-content {{
      font-size: 14px;
      line-height: 1.72;
      color: #334155;
      white-space: normal;
    }}
    .ai-markdown p {{ margin: 0 0 8px; }}
    .ai-markdown p:last-child {{ margin-bottom: 0; }}
    .ai-subtitle {{
      margin: 12px 0 6px;
      color: #4338ca;
      font-size: 14px;
      font-weight: 700;
    }}
    .ai-subtitle:first-child {{ margin-top: 0; }}
    .ai-markdown ol, .ai-markdown ul {{ margin: 4px 0 8px; padding-left: 1.35em; }}
    .ai-markdown li {{
      margin: 3px 0;
      color: #334155;
    }}
    .ai-markdown strong {{
      color: #1f2937;
      font-weight: 700;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --page-bg: #000000;
        --text: #f5f5f7;
      }}
      .report {{ color: #f5f5f7 !important; }}
      .module-card,
      .inner-card {{
        background: #1c1c1e !important;
        border-color: rgba(84, 84, 88, 0.65) !important;
        box-shadow: none !important;
        color: #f5f5f7 !important;
      }}
      .new-section-title, .rss-section-title, .standalone-section-title {{
        background: transparent !important;
        color: #f5f5f7 !important;
      }}
      .rss-item {{
        background: #14201a !important;
        border-left-color: #22c55e !important;
      }}
      .rss-author {{ color: #86efac !important; }}
      .news-title, .new-item-title, .rss-title {{ color: #ffffff !important; }}
      .word-name, .feed-name, .standalone-name {{ color: #f5f5f7 !important; }}
      .source-name, .new-source-title {{ color: #d1d5db !important; }}
      .news-header, .rss-meta, .time-info, .count-info, .rss-time,
      .rss-section-count, .standalone-section-count, .rss-count, .feed-count, .word-count, .word-index, .standalone-count {{
        color: #8e8e93 !important;
      }}
      .news-number, .new-item-number, .new-item-rank, .rank-num {{
        background: #2c2c2e !important;
        color: #ebebf5 !important;
      }}
      .new-item-rank.top, .rank-num.top {{
        background: rgba(220, 38, 38, 0.22) !important;
        color: #fca5a5 !important;
      }}
      .new-item-rank.high, .rank-num.high {{
        background: rgba(234, 88, 12, 0.22) !important;
        color: #fdba74 !important;
      }}
      .news-link, .rss-link, .footer-link, .new-item-title a, .rss-title a, .news-title a {{
        color: #60a5fa !important;
      }}
      .word-header, .feed-header, .standalone-header,
      .news-item, .rss-item, .new-item {{
        border-bottom-color: rgba(84, 84, 88, 0.45) !important;
      }}
      .ai-section-shell {{
        background: linear-gradient(to bottom right, #0894ff 0%, #c959dd 34%, #ff2e54 68%, #ff9004) !important;
      }}
      .ai-section {{
        background: #1c1c1e !important;
        border: none !important;
        color: #f5f5f7 !important;
        box-shadow:
          inset 0 0 12px rgba(8, 148, 255, 0.18),
          inset 0 0 16px rgba(201, 89, 221, 0.14),
          inset 0 0 20px rgba(255, 46, 84, 0.12),
          inset 0 0 24px rgba(255, 144, 4, 0.10) !important;
      }}
      .ai-section-title {{
        color: #c7d2fe !important;
      }}
      .ai-block-title {{
        color: #c7d2fe !important;
      }}
      .ai-subtitle {{
        color: #a5b4fc !important;
      }}
      .ai-block-content,
      .ai-markdown li,
      .ai-markdown p {{
        color: #ebebf5 !important;
      }}
      .ai-markdown strong {{
        color: #e0e7ff !important;
      }}
      .ai-block + .ai-block {{
        border-top-color: rgba(84, 84, 88, 0.65) !important;
      }}
    }}
    @media (max-width: 640px) {{
      .page {{ padding: 0 8px 18px; }}
      .header {{
        padding: 14px 14px 12px;
        border-radius: 16px;
        margin-bottom: 8px;
      }}
      .header-title {{ font-size: 17px; margin-bottom: 6px; }}
      .header-meta {{ font-size: 11px; line-height: 1.35; column-gap: 14px; row-gap: 1px; }}
      .report {{ gap: 8px; }}
      .module-card {{
        padding: 12px;
        border-radius: 16px;
      }}
      .inner-card {{
        padding: 8px 10px 6px;
        border-radius: 16px;
      }}
      .ai-section-shell {{
        border-radius: 16px;
        padding: 2.5px;
      }}
      .ai-section {{
        border-radius: 13.5px;
        padding: 12px;
      }}
      .new-section-title, .rss-section-title, .standalone-section-title {{
        font-size: 15px;
        margin: 0 10px 8px;
        padding: 0;
      }}
      .rss-item {{ border-radius: 16px; }}
      .news-title, .new-item-title, .rss-title, .ai-block-content {{ font-size: 14px; line-height: 1.52; }}
      .new-item {{ grid-template-columns: 18px 26px 1fr; column-gap: 6px; align-items: center; }}
      .news-number, .new-item-number {{ width: 18px; height: 18px; font-size: 11px; }}
      .new-item-rank, .rank-num {{ min-width: 22px; height: 15px; line-height: 15px; font-size: 9px; padding: 0 2px; }}
      .source-name {{ font-size: 12px; }}
    }}
</style>
</head>
<body>
  <div class="page">
    <div class="container">
      <div class="header">
        <div class="header-title">热点新闻分析</div>
        <div class="header-meta">
          <div class="meta-item"><strong>报告类型</strong>：{html_escape(mode_label)}</div>
          <div class="meta-item"><strong>分析模型</strong>：{html_escape(analysis_model or '未启用')}</div>
          <div class="meta-item"><strong>生成时间</strong>：{html_escape(generated_at)}</div>
        </div>
      </div>
      <div class="content">
        <div class="report">{report_body}</div>
      </div>
    </div>
  </div>
</body>
</html>
"""
