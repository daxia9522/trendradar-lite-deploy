# coding=utf-8
"""
存储后端抽象基类和数据模型

定义统一的存储接口，所有存储后端都需要实现这些方法
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class NewsItem:
    """新闻条目数据模型（热榜数据）"""

    title: str                          # 新闻标题
    source_id: str                      # 来源平台ID（如 toutiao, baidu）
    source_name: str = ""               # 来源平台名称（运行时使用，数据库不存储）
    rank: int = 0                       # 排名
    url: str = ""                       # 链接 URL
    mobile_url: str = ""                # 移动端 URL
    crawl_time: str = ""                # 抓取时间（HH:MM 格式）

    # 统计信息（用于分析）
    ranks: List[int] = field(default_factory=list)  # 历史排名列表
    first_time: str = ""                # 首次出现时间
    last_time: str = ""                 # 最后出现时间
    count: int = 1                      # 出现次数
    rank_timeline: List[Dict[str, Any]] = field(default_factory=list)  # 完整排名时间线
                                        # 格式: [{"time": "09:30", "rank": 1}, {"time": "10:00", "rank": 2}, ...]
                                        # None 表示脱榜: [{"time": "11:00", "rank": None}]


@dataclass
class RSSItem:
    """RSS 条目数据模型"""

    title: str                          # 标题
    feed_id: str                        # RSS 源 ID（如 "hacker-news"）
    feed_name: str = ""                 # RSS 源名称（运行时使用）
    url: str = ""                       # 文章链接
    guid: str = ""                      # RSS guid 或 Atom id
    published_at: str = ""              # RSS 发布时间（ISO 格式）
    summary: str = ""                   # 摘要/描述
    author: str = ""                    # 作者
    crawl_time: str = ""                # 抓取时间（HH:MM 格式）

    # 统计信息
    first_time: str = ""                # 首次抓取时间
    last_time: str = ""                 # 最后抓取时间
    count: int = 1                      # 抓取次数


    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RSSItem":
        """从字典创建"""
        return cls(
            title=data.get("title", ""),
            feed_id=data.get("feed_id", ""),
            feed_name=data.get("feed_name", ""),
            url=data.get("url", ""),
            guid=data.get("guid", ""),
            published_at=data.get("published_at", ""),
            summary=data.get("summary", ""),
            author=data.get("author", ""),
            crawl_time=data.get("crawl_time", ""),
            first_time=data.get("first_time", ""),
            last_time=data.get("last_time", ""),
            count=data.get("count", 1),
        )


@dataclass
class RSSData:
    """
    RSS 数据集合

    结构:
    - date: 日期（YYYY-MM-DD）
    - crawl_time: 抓取时间（HH:MM）
    - items: 按 feed_id 分组的 RSS 条目
    - id_to_name: feed_id 到名称的映射
    - failed_ids: 失败的 feed_id 列表
    """

    date: str                                   # 日期
    crawl_time: str                             # 抓取时间
    items: Dict[str, List[RSSItem]]             # 按 feed_id 分组的条目
    id_to_name: Dict[str, str] = field(default_factory=dict)   # ID到名称映射
    failed_ids: List[str] = field(default_factory=list)        # 失败的ID


@dataclass
class NewsData:
    """
    新闻数据集合

    结构:
    - date: 日期（YYYY-MM-DD）
    - crawl_time: 抓取时间（HH时MM分）
    - items: 按来源ID分组的新闻条目
    - id_to_name: 来源ID到名称的映射
    - failed_ids: 失败的来源ID列表
    """

    date: str                                   # 日期
    crawl_time: str                             # 抓取时间
    items: Dict[str, List[NewsItem]]            # 按来源分组的新闻
    id_to_name: Dict[str, str] = field(default_factory=dict)   # ID到名称映射
    failed_ids: List[str] = field(default_factory=list)        # 失败的ID


class StorageBackend(ABC):
    """
    存储后端抽象基类

    所有存储后端都需要实现这些方法，以支持:
    - 保存新闻数据
    - 保存可选的 TXT 快照
    """

    @abstractmethod
    def save_news_data(self, data: NewsData) -> bool:
        """
        保存新闻数据

        Args:
            data: 新闻数据

        Returns:
            是否保存成功
        """
        pass


    @abstractmethod
    def save_txt_snapshot(self, data: NewsData) -> Optional[str]:
        """
        保存 TXT 快照（可选功能，本地环境可用）

        Args:
            data: 新闻数据

        Returns:
            保存的文件路径，如果不支持返回 None
        """
        pass


    @abstractmethod
    def cleanup(self) -> None:
        """
        清理资源（如临时文件、数据库连接等）
        """
        pass

    @abstractmethod
    def cleanup_old_data(self, retention_days: int) -> int:
        """
        清理过期数据

        Args:
            retention_days: 保留天数（0 表示不清理）

        Returns:
            删除的日期目录数量
        """
        pass

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """
        存储后端名称
        """
        pass

    @property
    @abstractmethod
    def supports_txt(self) -> bool:
        """
        是否支持生成 TXT 快照
        """
        pass

    # === 时间段执行记录（调度系统）===

    def has_period_executed(self, date_str: str, period_key: str, action: str) -> bool:
        """
        检查指定时间段的某个 action 是否已执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否已执行
        """
        return False

    def record_period_execution(self, date_str: str, period_key: str, action: str) -> bool:
        """
        记录时间段的 action 执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否记录成功
        """
        return False

    # === 批量写入与同步 ===

    def begin_batch(self) -> None:
        """开启批量模式（远程后端延迟上传，本地后端无操作）"""
        pass

    def flush(self) -> bool:
        """将批量模式下积累的变更同步到后端。"""
        return True

    def end_batch(self) -> bool:
        """结束批量模式并同步积累的变更。"""
        return self.flush()


def convert_crawl_results_to_news_data(
    results: Dict[str, Dict],
    id_to_name: Dict[str, str],
    failed_ids: List[str],
    crawl_time: str,
    crawl_date: str,
) -> NewsData:
    """
    将爬虫结果转换为 NewsData 格式

    Args:
        results: 爬虫返回的结果 {source_id: {title: {ranks: [], url: "", mobileUrl: ""}}}
        id_to_name: 来源ID到名称的映射
        failed_ids: 失败的来源ID
        crawl_time: 抓取时间（HH:MM）
        crawl_date: 抓取日期（YYYY-MM-DD）

    Returns:
        NewsData 对象
    """
    items = {}

    for source_id, titles_data in results.items():
        source_name = id_to_name.get(source_id, source_id)
        news_list = []

        for title, data in titles_data.items():
            ranks = data.get("ranks", [])
            url = data.get("url", "")
            mobile_url = data.get("mobileUrl", "")

            rank = ranks[0] if ranks else 99

            news_item = NewsItem(
                title=title,
                source_id=source_id,
                source_name=source_name,
                rank=rank,
                url=url,
                mobile_url=mobile_url,
                crawl_time=crawl_time,
                ranks=ranks,
                first_time=crawl_time,
                last_time=crawl_time,
                count=1,
            )
            news_list.append(news_item)

        items[source_id] = news_list

    return NewsData(
        date=crawl_date,
        crawl_time=crawl_time,
        items=items,
        id_to_name=id_to_name,
        failed_ids=failed_ids,
    )
