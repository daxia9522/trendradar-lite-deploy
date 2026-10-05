"""Daily collection/persistence I/O, independent of the runner's state."""

from pathlib import Path
from typing import Callable, Dict, List, Optional

from trendradar.storage import convert_crawl_results_to_news_data
from trendradar.utils.time import DEFAULT_TIMEZONE

from .models import CrawlResult, RSSCollection


def crawl_hotlist(
    platforms: List[Dict],
    data_fetcher,
    storage,
    *,
    request_interval: int,
    format_time: Callable[[], str],
    format_date: Callable[[], str],
) -> CrawlResult:
    """执行数据爬取"""
    ids = []
    domain_rules = {}
    for platform in platforms:
        if "name" in platform:
            ids.append((platform["id"], platform["name"]))
        else:
            ids.append(platform["id"])

        expected_domain = str(platform.get("expected_domain", "")).strip()
        if expected_domain:
            domain_rules[platform["id"]] = expected_domain

    print(
        f"配置的监控平台: {[p.get('name', p['id']) for p in platforms]}"
    )
    print(f"开始爬取数据，请求间隔 {request_interval} 毫秒")
    Path("output").mkdir(parents=True, exist_ok=True)

    results, id_to_name, failed_ids = data_fetcher.crawl_websites(
        ids,
        request_interval=request_interval,
        domain_rules=domain_rules,
    )

    # 转换为 NewsData 格式并保存到存储后端
    crawl_time = format_time()
    crawl_date = format_date()
    news_data = convert_crawl_results_to_news_data(
        results, id_to_name, failed_ids, crawl_time, crawl_date
    )

    # 保存到存储后端（SQLite）
    if storage.save_news_data(news_data):
        print(f"数据已保存到存储后端: {storage.backend_name}")
    else:
        raise RuntimeError("Hotlist persistence failed; report capture stopped")

    # 保存 TXT 快照（如果启用）
    txt_file = storage.save_txt_snapshot(news_data)
    if txt_file:
        print(f"TXT 快照已保存: {txt_file}")

    return CrawlResult(results, id_to_name, failed_ids)


def build_rss_fetcher_config(
    rss_feeds: List[Dict],
    rss_config: Dict,
    *,
    proxy_url: Optional[str] = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> Dict:
    """Adapt normalized uppercase app config without mutating source feeds."""
    freshness_config = rss_config.get("FRESHNESS_FILTER", {})
    feeds = []
    for feed_config in rss_feeds:
        normalized = dict(feed_config)
        normalized.setdefault("max_items", 50)
        feeds.append(normalized)
    return {
        "feeds": feeds,
        "request_interval": rss_config.get("REQUEST_INTERVAL", 2000),
        "timeout": rss_config.get("TIMEOUT", 15),
        "use_proxy": rss_config.get("USE_PROXY", False),
        "proxy_url": rss_config.get("PROXY_URL", "") or proxy_url or "",
        "timezone": timezone,
        "freshness_filter": {
            "enabled": freshness_config.get("ENABLED", True),
            "max_age_days": freshness_config.get("MAX_AGE_DAYS", 3),
        },
    }


def create_rss_fetcher(
    rss_feeds: List[Dict],
    rss_config: Dict,
    *,
    proxy_url: Optional[str] = None,
    timezone: str = DEFAULT_TIMEZONE,
):
    """Construct only on the enabled RSS path; keep optional import failures local."""
    from trendradar.crawler.rss import RSSFetcher

    return RSSFetcher.from_config(build_rss_fetcher_config(
        rss_feeds, rss_config, proxy_url=proxy_url, timezone=timezone,
    ))


def crawl_rss(
    *,
    enabled: bool,
    get_feeds: Callable[[], List[Dict]],
    create_fetcher: Callable,
    save_rss_data: Callable,
) -> RSSCollection:
    """Collect/persist RSS only; views and novelty come from frozen capture."""
    if not enabled:
        return RSSCollection()
    rss_feeds = get_feeds()
    if not rss_feeds:
        print("[RSS] 未配置任何 RSS 源")
        return RSSCollection()
    try:
        fetcher = create_fetcher(rss_feeds)
        if not fetcher.feeds:
            return RSSCollection()
        rss_data = fetcher.fetch_all()
        if not save_rss_data(rss_data):
            print("[RSS] 数据保存失败，保留上次发布的 RSS 覆盖边界")
            return RSSCollection()
        print("[RSS] 数据已保存到存储后端")
        return RSSCollection(not rss_data.failed_ids, tuple(rss_data.failed_ids))
    except Exception as exc:
        print(f"[RSS] 采集不可用，保留原覆盖边界（{type(exc).__name__}）")
        return RSSCollection()
