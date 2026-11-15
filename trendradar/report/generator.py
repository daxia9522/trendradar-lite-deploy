# coding=utf-8
"""展示数据准备与HTML快照生成。"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable
import unicodedata

from trendradar.core.analyzer import calculate_news_weight
from trendradar.utils.url import normalize_url


def _select_new_titles(new_titles, stats, id_to_name, options, rank_threshold, weight_config):
    """首次发现时间优先，同批次按热度排序。"""
    if options is None:
        return new_titles
    total, per_source = options.get("MAX_ITEMS", 0), options.get("MAX_PER_SOURCE", 0)
    weights = weight_config or {"RANK_WEIGHT": 0.6, "FREQUENCY_WEIGHT": 0.3, "HOTNESS_WEIGHT": 0.1}
    name_to_id = {name: sid for sid, name in id_to_name.items()}

    def identities(title, data, source_id):
        normalized = " ".join(unicodedata.normalize("NFKC", title).split()).casefold()
        keys = {("title", normalized)} if normalized else set()
        for field in ("url", "mobileUrl", "mobile_url"):
            if data.get(field):
                keys.add(("url", normalize_url(data[field], source_id)))
        return keys

    main_keys = {key for group in stats for item in group.get("titles", [])
                 for key in identities(item["title"], item, name_to_id.get(item.get("source_name"), ""))}
    rows = []
    for sid, titles in new_titles.items():
        for title, data in titles.items():
            ranks = list(dict.fromkeys(r for r in (data.get("ranks") or []) if type(r) is int and r > 0))
            try:
                count = max(1, int(data.get("count", len(ranks) or 1)))
            except (TypeError, ValueError):
                count = 1
            score = round(calculate_news_weight({"ranks": ranks, "count": count}, rank_threshold, weights), 8)
            try:
                first_seen = datetime.fromisoformat(data.get("first_seen_at", "")).timestamp()
            except (TypeError, ValueError, OverflowError):
                first_seen = 0
            rows.append((-first_seen, -score, sid, title, data.get("url", ""), data))

    selected, seen, shown = {}, set(), 0
    for _, _, sid, title, _, data in sorted(rows, key=lambda row: row[:5]):
        keys = identities(title, data, sid)
        if keys & (main_keys | seen):
            continue
        if per_source > 0 and len(selected.get(sid, {})) >= per_source:
            continue
        selected.setdefault(sid, {})[title] = data
        seen.update(keys)
        shown += 1
        if total > 0 and shown >= total:
            break
    return {sid: selected[sid] for sid in new_titles if sid in selected}


def prepare_report_data(
    stats: List[Dict],
    failed_ids: Optional[List] = None,
    new_titles: Optional[Dict] = None,
    id_to_name: Optional[Dict] = None,
    mode: str = "daily",
    rank_threshold: int = 3,
    show_new_section: bool = True,
    new_items_options: Optional[Dict] = None,
    weight_config: Optional[Dict] = None,
) -> Dict:
    """将已归类的主榜和新增候选整理为展示数据。"""
    processed_new_titles = []
    if mode != "incremental" and show_new_section and new_titles and id_to_name:
        selected = _select_new_titles(new_titles, stats, id_to_name, new_items_options,
                                      rank_threshold, weight_config)
        print(f"[新增展示] {sum(len(titles) for titles in selected.values())} 条")
        for source_id, titles in selected.items():
            if not titles:
                continue
            source_name = id_to_name.get(source_id, source_id)
            processed_new_titles.append({
                "source_id": source_id,
                "source_name": source_name,
                "titles": [{
                    "title": title,
                    "source_name": source_name,
                    "time_display": "",
                    "count": 1,
                    "ranks": data.get("ranks", []),
                    "rank_threshold": rank_threshold,
                    "url": data.get("url", ""),
                    "mobile_url": data.get("mobileUrl", ""),
                    "is_new": True,
                } for title, data in titles.items()],
            })

    processed_stats = []
    for group in stats:
        if group["count"] <= 0:
            continue
        processed_stats.append({
            "word": group["word"],
            "count": group["count"],
            "percentage": group.get("percentage", 0),
            "titles": [{
                "title": item["title"],
                "source_name": item["source_name"],
                "time_display": item["time_display"],
                "count": item["count"],
                "ranks": item["ranks"],
                "rank_threshold": item["rank_threshold"],
                "url": item.get("url", ""),
                "mobile_url": item.get("mobileUrl", ""),
                "is_new": item.get("is_new", False),
                "matched_keyword": item.get("matched_keyword", ""),
            } for item in group["titles"]],
        })
    return {
        "stats": processed_stats,
        "new_titles": processed_new_titles,
        "failed_ids": failed_ids or [],
        "total_new_count": sum(len(source["titles"]) for source in processed_new_titles),
    }


def generate_html_report(
    report_data: Dict,
    mode: str = "daily",
    output_dir: str = "output",
    date_folder: str = "",
    time_filename: str = "",
    render_email_html_func: Optional[Callable] = None,
) -> str:
    """渲染并保存本轮HTML快照。"""
    snapshot_path = Path(output_dir) / "html" / date_folder
    snapshot_path.mkdir(parents=True, exist_ok=True)
    snapshot_file = str(snapshot_path / f"{time_filename}.html")
    if not render_email_html_func:
        raise ValueError("render_email_html_func is required")
    html_content = render_email_html_func(report_data, mode)
    Path(snapshot_file).write_text(html_content, encoding="utf-8")
    return snapshot_file
