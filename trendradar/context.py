# coding=utf-8
"""应用上下文模块"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from trendradar.utils.time import (
    DEFAULT_TIMEZONE,
    get_configured_time,
    format_date_folder,
    format_time_filename,
    convert_time_for_display,
)
from trendradar.core import (
    load_frequency_words,
    count_word_frequency,
    Scheduler,
)
from trendradar.report import generate_html_report
from trendradar.report.daily import render_daily_html
from trendradar.notification import NotificationDispatcher
from trendradar.storage import get_storage_manager, storage_manager_options


class AppContext:
    """应用上下文类"""

    def __init__(self, config: Dict[str, Any]):
        """初始化应用上下文"""
        self.config = config
        self._storage_manager = None
        self._scheduler = None

    # === 配置访问 ===

    @property
    def timezone(self) -> str:
        """获取配置的时区"""
        return self.config.get("TIMEZONE", DEFAULT_TIMEZONE)

    @property
    def rank_threshold(self) -> int:
        """获取排名阈值"""
        return self.config.get("RANK_THRESHOLD", 50)

    @property
    def weight_config(self) -> Dict:
        """获取权重配置"""
        return self.config.get("WEIGHT_CONFIG", {})

    @property
    def platforms(self) -> List[Dict]:
        """获取平台配置列表"""
        return self.config.get("PLATFORMS", [])

    @property
    def platform_ids(self) -> List[str]:
        """获取平台ID列表"""
        return [p["id"] for p in self.platforms]

    @property
    def rss_config(self) -> Dict:
        """获取 RSS 配置"""
        return self.config.get("RSS", {})

    @property
    def rss_enabled(self) -> bool:
        """RSS 是否启用"""
        return self.rss_config.get("ENABLED", False)

    @property
    def rss_feeds(self) -> List[Dict]:
        """获取 RSS 源列表"""
        return self.rss_config.get("FEEDS", [])

    @property
    def display_mode(self) -> str:
        """获取显示模式 (keyword | platform)"""
        return self.config.get("DISPLAY_MODE", "keyword")

    @property
    def show_new_section(self) -> bool:
        """是否显示新增热点区域"""
        return self.config.get("DISPLAY", {}).get("REGIONS", {}).get("NEW_ITEMS", True)

    @property
    def region_order(self) -> List[str]:
        """获取区域显示顺序"""
        default_order = ["hotlist", "rss", "new_items", "standalone", "ai_analysis"]
        return self.config.get("DISPLAY", {}).get("REGION_ORDER", default_order)

    # === 时间操作 ===

    def get_time(self) -> datetime:
        """获取当前配置时区的时间"""
        return get_configured_time(self.timezone)

    def format_date(self) -> str:
        """格式化日期文件夹 (YYYY-MM-DD)"""
        return format_date_folder(timezone=self.timezone)

    def format_time(self) -> str:
        """格式化时间文件名 (HH-MM)"""
        return format_time_filename(self.timezone)

    @staticmethod
    def convert_time_display(time_str: str) -> str:
        """将 HH-MM 转换为 HH:MM"""
        return convert_time_for_display(time_str)

    # === 存储操作 ===

    def get_storage_manager(self):
        """获取存储管理器（延迟初始化，单例）"""
        if self._storage_manager is None:
            self._storage_manager = get_storage_manager(**storage_manager_options(self.config))
        return self._storage_manager

    # === 数据处理 ===

    def create_daily_source_reader(self):
        """Read report inputs from the shared daily source."""
        from trendradar.daily_flow.capture import DailySourceReader
        return DailySourceReader(source=self.get_storage_manager().get_day_source())


    # === 频率词处理 ===

    def load_frequency_words(
        self, frequency_file: Optional[str] = None
    ) -> Tuple[List[Dict], List[str], List[str]]:
        """加载频率词配置"""
        return load_frequency_words(frequency_file)


    # === 统计分析 ===

    def count_frequency(
        self,
        results: Dict,
        word_groups: List[Dict],
        filter_words: List[str],
        id_to_name: Dict,
        title_info: Optional[Dict] = None,
        new_titles: Optional[Dict] = None,
        mode: str = "daily",
        global_filters: Optional[List[str]] = None,
        quiet: bool = False,
        max_news_per_keyword: Optional[int] = None,
        group_resolver=None,
    ) -> Tuple[List[Dict], int]:
        """统计词频"""
        if max_news_per_keyword is None:
            max_news_per_keyword = self.config.get("MAX_NEWS_PER_KEYWORD", 0)
        return count_word_frequency(
            results=results,
            word_groups=word_groups,
            filter_words=filter_words,
            id_to_name=id_to_name,
            title_info=title_info,
            rank_threshold=self.rank_threshold,
            new_titles=new_titles,
            mode=mode,
            global_filters=global_filters,
            weight_config=self.weight_config,
            max_news_per_keyword=max_news_per_keyword,
            sort_by_position_first=self.config.get("SORT_BY_POSITION_FIRST", False),
            # Daily novelty is publication-relative; never reread mutable
            # crawl history or turn midnight into an implicit novelty reset.
            is_first_crawl_func=lambda: False,
            convert_time_func=self.convert_time_display,
            quiet=quiet,
            group_resolver=group_resolver,
        )

    # === 报告生成 ===

    def generate_html(
        self,
        report_data: Dict,
        mode: str = "daily",
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        ai_analysis: Optional[Any] = None,
        standalone_data: Optional[Dict] = None,
        captured_at: Optional[str] = None,
    ) -> str:
        """将展示数据渲染为HTML快照。"""
        return generate_html_report(
            report_data=report_data, mode=mode, output_dir="output",
            date_folder=captured_at[:10] if captured_at else self.format_date(),
            time_filename=(datetime.fromisoformat(captured_at).strftime('%H-%M-%S-%f')
                           if captured_at else self.format_time()),
            render_email_html_func=lambda data, report_mode: self.render_email_html(
                data, report_mode, rss_items=rss_items, rss_new_items=rss_new_items,
                ai_analysis=ai_analysis, standalone_data=standalone_data, captured_at=captured_at,
            ),
        )

    def render_email_html(
        self,
        report_data: Dict,
        mode: str = "daily",
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        ai_analysis: Optional[Any] = None,
        standalone_data: Optional[Dict] = None,
        captured_at: Optional[str] = None,
    ) -> str:
        """渲染邮箱专用 HTML 内容"""
        # Header「分析模型」展示本次实际调用成功的模型，而非仅配置主模型
        analysis_model = ""
        if ai_analysis is not None:
            if getattr(ai_analysis, "success", False):
                analysis_model = str(getattr(ai_analysis, "model", "") or "").strip()
                analysis_model = analysis_model[:1].upper() + analysis_model[1:]
            else:
                analysis_model = "分析失败"
        generated_at = self.get_time().strftime("%Y-%m-%d %H:%M:%S")
        return render_daily_html(
            report_data=report_data,
            mode=mode,
            region_order=self.region_order,
            generated_at=generated_at,
            rss_items=rss_items,
            rss_new_items=rss_new_items,
            display_mode=self.display_mode,
            ai_analysis=ai_analysis,
            analysis_model=analysis_model,
            show_new_section=self.show_new_section,
            standalone_data=standalone_data,
            parse_timestamp=datetime.fromisoformat,
        )

    # === 通知发送 ===

    def create_notification_dispatcher(self) -> NotificationDispatcher:
        """创建邮件通知调度器。"""
        return NotificationDispatcher(
            config=self.config,
            get_time_func=self.get_time,
        )

    def create_scheduler(self) -> Scheduler:
        """创建调度器（延迟初始化，单例）"""
        if self._scheduler is None:
            schedule_config = self.config.get("SCHEDULE", {})
            timeline_data = self.config.get("_TIMELINE_DATA", {})

            self._scheduler = Scheduler(
                schedule_config=schedule_config,
                timeline_data=timeline_data,
                storage_backend=self.get_storage_manager(),
                get_time_func=self.get_time,
                fallback_report_mode=self.config.get("REPORT_MODE", "current"),
            )
        return self._scheduler

    # === 资源清理 ===

    def cleanup(self):
        """清理资源"""
        if self._storage_manager:
            self._storage_manager.cleanup_old_data()
            self._storage_manager.cleanup()
            self._storage_manager = None
