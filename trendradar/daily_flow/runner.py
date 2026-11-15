# coding=utf-8
"""Daily application runner: order, action gates and resource lifetime."""

import os
from copy import deepcopy
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

from trendradar import __version__
from trendradar.ai import AIAnalyzer, AIAnalysisResult
from trendradar.context import AppContext
from trendradar.core import load_config
from trendradar.core.analyzer import convert_keyword_stats_to_platform_stats
from trendradar.core.execution_policy import ExecutionPolicy, is_manual_force_run
from trendradar.core.scheduler import ResolvedSchedule
from trendradar.crawler import DataFetcher
from trendradar.utils.time import DEFAULT_TIMEZONE, calculate_days_old, is_within_days

from . import collection, inputs, rss
from .capture import capture_sources
from .publication import PublicationBeacon
from .models import CrawlResult, KeywordRules, PreparedReportInput, ReportArtifacts, RSSResult, RSSCollection


class DailyRunner:
    """Coordinate explicit collection/input steps, then AI, HTML and delivery."""

    MODE_STRATEGIES = {
        "incremental": {
            "description": "增量模式（只关注新增新闻，无新增时不推送）",
            "report_type": "增量分析",
        },
        "current": {
            "description": "当前榜单模式（当前榜单匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "当前榜单",
        },
        "daily": {
            "description": "全天汇总模式（所有匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "全天汇总",
        },
    }

    def __init__(self, config: Optional[Dict] = None):
        # 使用传入的配置或加载新配置
        if config is None:
            print("正在加载配置...")
            config = load_config()
        print(f"TrendRadar v{__version__} 配置加载完成")
        print(f"监控平台数量: {len(config['PLATFORMS'])}")
        print(f"时区: {config.get('TIMEZONE', DEFAULT_TIMEZONE)}")

        # 创建应用上下文
        self.ctx = AppContext(config)

        self.request_interval = self.ctx.config["REQUEST_INTERVAL"]
        self.report_mode = self.ctx.config["REPORT_MODE"]
        self.frequency_file = None
        self.rank_threshold = self.ctx.rank_threshold
        self.is_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        self.is_docker_container = self._detect_docker_environment()
        self.proxy_url = None
        self._setup_proxy()
        self.data_fetcher = DataFetcher(
            self.proxy_url,
            api_url=self.ctx.config.get("PLATFORMS_API_URL") or None,
            api_fallback_urls=self.ctx.config.get("PLATFORMS_API_FALLBACK_URLS") or None,
        )

        # 初始化存储管理器（使用 AppContext）
        self._init_storage_manager()

    def _init_storage_manager(self) -> None:
        """初始化存储管理器（使用 AppContext）"""
        # 获取数据保留天数（支持环境变量覆盖）
        env_retention = os.environ.get("STORAGE_RETENTION_DAYS", "").strip()
        if env_retention:
            # 环境变量覆盖配置
            self.ctx.config["STORAGE"]["RETENTION_DAYS"] = int(env_retention)

        self.storage_manager = self.ctx.get_storage_manager()
        print(f"存储后端: {self.storage_manager.backend_name}")

        retention_days = self.ctx.config.get("STORAGE", {}).get("RETENTION_DAYS", 0)
        if retention_days > 0:
            print(f"数据保留天数: {retention_days} 天")

    def _detect_docker_environment(self) -> bool:
        """检测是否运行在 Docker 容器中"""
        try:
            if os.environ.get("DOCKER_CONTAINER") == "true":
                return True

            if os.path.exists("/.dockerenv"):
                return True

            return False
        except Exception:
            return False

    def _should_open_browser(self) -> bool:
        """判断是否应该打开浏览器"""
        if self.is_github_actions or self.is_docker_container:
            return False
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def _setup_proxy(self) -> None:
        """设置代理配置"""
        if not self.is_github_actions and self.ctx.config["USE_PROXY"]:
            self.proxy_url = self.ctx.config["DEFAULT_PROXY"]
            print("本地环境，使用代理")
        elif not self.is_github_actions and not self.ctx.config["USE_PROXY"]:
            print("本地环境，未启用代理")
        else:
            print("GitHub Actions环境，不使用代理")

    def _get_mode_strategy(self) -> Dict:
        """获取当前模式的策略配置"""
        return self.MODE_STRATEGIES.get(self.report_mode, self.MODE_STRATEGIES["daily"])

    def _has_notification_configured(self) -> bool:
        """检查是否配置了邮件通知（GA3 仅支持邮件）"""
        cfg = self.ctx.config
        return bool(
            cfg.get("EMAIL_FROM")
            and cfg.get("EMAIL_PASSWORD")
            and cfg.get("EMAIL_TO")
        )

    def _manual_force_run(self) -> bool:
        """Read the current environment's explicit manual-run markers."""
        return is_manual_force_run(os.environ)

    def _action_allowed(self, schedule: ResolvedSchedule, action: str) -> bool:
        """Read once state only when eligible; never record an execution here."""
        policy = ExecutionPolicy(
            scheduled=getattr(schedule, action),
            once=getattr(schedule, f"once_{action}"),
            period_key=schedule.period_key,
            manual_force=self._manual_force_run(),
        )
        prefix, description, verb, proceed = {
            "analyze": ("[AI]", " AI 分析", "分析", "继续分析"),
            "push": ("[推送]", "推送", "推送", "继续发送"),
        }[action]
        if not policy.scheduled:
            if policy.manual_force:
                print(f"{prefix} 调度器: 当前时间段原本不执行{description}，但当前为手动触发，{proceed}")
            else:
                print(f"{prefix} 调度器: 当前时间段不执行{description}")
                return False

        already_executed = False
        if policy.check_history:
            if action == "push":
                already_executed = self._publication_beacon().load().get("window") == self._publication_window(schedule)
            else:
                already_executed = self.ctx.create_scheduler().already_executed(
                    schedule.period_key, action, self.ctx.format_date())
            period = schedule.period_name or schedule.period_key
            if already_executed:
                if policy.manual_force:
                    print(f"{prefix} 调度器: 时间段 {period} 今天已{verb}过，但当前为手动触发，{proceed}")
                else:
                    print(f"{prefix} 调度器: 时间段 {period} 今天已{verb}过，跳过")
            else:
                print(f"{prefix} 调度器: 时间段 {period} 今天首次{verb}")
        return policy.allows(already_executed)

    def _run_ai_analysis(
        self,
        stats: List[Dict],
        rss_items: Optional[List[Dict]],
        mode: str,
        report_type: str,
        schedule: ResolvedSchedule = None,
    ) -> Optional[AIAnalysisResult]:
        """执行 AI 分析"""
        analysis_config = self.ctx.config.get("AI_ANALYSIS", {})
        if not analysis_config.get("ENABLED", False):
            return None

        if not self._action_allowed(schedule, "analyze"):
            return None

        print("[AI] 正在进行 AI 分析...")
        try:
            ai_config = self.ctx.config.get("AI", {})
            debug_mode = self.ctx.config.get("DEBUG", False)
            analyzer = AIAnalyzer(ai_config, analysis_config, self.ctx.get_time, debug=debug_mode)

            # 来源范围只取实际分析材料，不注入独立速览的来源样本。
            source_names = set()
            for stat in (stats or []) + (rss_items or []):
                for item in stat.get("titles", []) or []:
                    source = item.get("feed_name", item.get("source_name", item.get("source", "")))
                    if source:
                        source_names.add(str(source))
            platforms = sorted(source_names)
            keywords = [s.get("word", "") for s in stats if s.get("word")] if stats else []
            keywords.extend(
                s.get("word", "") for s in (rss_items or []) if s.get("word")
            )
            keywords = list(dict.fromkeys(keywords))

            result = analyzer.analyze(
                stats=stats,
                rss_stats=rss_items,
                report_mode=mode,
                report_type=report_type,
                platforms=platforms,
                keywords=keywords,
            )

            if result.success:
                if result.error:
                    # 成功但有非致命警告
                    print(f"[AI] 分析完成（有警告: {result.error}）")
                else:
                    print("[AI] 分析完成")

                # 记录 AI 分析
                if schedule.once_analyze and schedule.period_key:
                    scheduler = self.ctx.create_scheduler()
                    date_str = self.ctx.format_date()
                    scheduler.record_execution(schedule.period_key, "analyze", date_str)
            else:
                print(f"[AI] 分析失败: {result.error}")

            return result
        except Exception as e:
            import traceback
            error_type = type(e).__name__
            error_msg = str(e)
            # 截断过长的错误消息
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            print(f"[AI] 分析出错 ({error_type}): {error_msg}")
            # 详细错误日志到 stderr
            import sys
            print(f"[AI] 详细错误堆栈:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return AIAnalysisResult(success=False, error=f"{error_type}: {error_msg}")

    def _send_notification_if_needed(self, prepared, artifacts, schedule) -> bool:
        """Advance the local cutoff only after a successful send."""
        if not self.ctx.config.get("ENABLE_NOTIFICATION", False) or not self._has_notification_configured():
            return False
        has_content = artifacts.has_visible_news is True
        if not has_content or not artifacts.html_file:
            print("[推送] 无有效内容或未生成 HTML，不更新信标")
            return False
        receipt = self.ctx.create_notification_dispatcher().send_report(
            report_type=self._get_mode_strategy()["report_type"],
            html_file_path=artifacts.html_file,
            period_name=schedule.period_name if schedule else None,
        )
        if receipt.sent:
            capture = prepared.capture.to_dict()
            self._publication_beacon().advance(
                capture, self._publication_window(schedule, capture["captured_at"]))
        return receipt.sent

    def _publication_beacon(self):
        if getattr(self, "_beacon", None) is None:
            self._beacon = PublicationBeacon(self.storage_manager.data_dir)
        return self._beacon

    def _publication_window(self, schedule, captured_at=None):
        now = self.ctx.get_time()
        period = (schedule.period_key if schedule else None) or "scheduled"
        if schedule and not schedule.once_push:
            period += ":" + now.strftime("%H-%M")
        date = captured_at[:10] if captured_at else now.date().isoformat()
        return date + ":" + period

    def _initialize_and_check_config(self) -> bool:
        """通用初始化和配置检查"""
        now = self.ctx.get_time()
        print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if not self.ctx.config["ENABLE_CRAWLER"]:
            print("爬虫功能已禁用（ENABLE_CRAWLER=False）")

        has_notification = self._has_notification_configured()
        if not self.ctx.config["ENABLE_NOTIFICATION"]:
            print("通知功能已禁用（ENABLE_NOTIFICATION=False），将只进行数据抓取")
        elif not has_notification:
            print("未配置任何通知渠道，将只进行数据抓取，不发送通知")
        else:
            print("通知功能已启用，将发送通知")

        return True

    def _resolve_schedule(self) -> ResolvedSchedule:
        """在数据采集前解析并应用本轮调度。"""
        schedule = self.ctx.create_scheduler().resolve()

        effective_mode = schedule.report_mode
        if effective_mode != self.report_mode:
            print(f"[调度] 报告模式覆盖: {self.report_mode} -> {effective_mode}")
        self.report_mode = effective_mode
        self.frequency_file = schedule.frequency_file

        mode_strategy = self._get_mode_strategy()
        print(f"报告模式: {self.report_mode}")
        print(f"运行模式: {mode_strategy['description']}")
        return schedule

    def _crawl_data(self) -> CrawlResult:
        return collection.crawl_hotlist(
            self.ctx.platforms, self.data_fetcher, self.storage_manager,
            request_interval=self.request_interval,
            format_time=self.ctx.format_time, format_date=self.ctx.format_date,
        )

    def _create_rss_fetcher(self, rss_feeds: List[Dict]):
        return collection.create_rss_fetcher(
            rss_feeds, self.ctx.rss_config, proxy_url=self.proxy_url,
            timezone=self.ctx.config.get("TIMEZONE", DEFAULT_TIMEZONE),
        )

    def _crawl_rss_data(self) -> RSSCollection:
        return collection.crawl_rss(
            enabled=self.ctx.rss_enabled, get_feeds=lambda: self.ctx.rss_feeds,
            create_fetcher=self._create_rss_fetcher,
            save_rss_data=lambda data: self.storage_manager.save_rss_data(data),
        )

    def _process_rss_data_by_mode(self, capture, keywords, *, defer_grouping=False) -> RSSResult:
        return rss.prepare_rss_input(
            capture, mode=self.report_mode, config=self.ctx.config, keywords=keywords,
            convert_items=self._convert_rss_items_to_list,
            timezone=self.ctx.timezone, rank_threshold=self.rank_threshold,
            defer_grouping=defer_grouping,
        )

    def _convert_rss_items_to_list(
        self, items_dict: Dict, id_to_name: Dict,
        *, within_days=is_within_days, days_old=calculate_days_old,
    ) -> List[Dict]:
        return rss.convert_rss_items_to_list(
            items_dict, id_to_name, rss_config=self.ctx.rss_config,
            rss_feeds=self.ctx.rss_feeds, config=self.ctx.config,
            within_days=within_days, days_old=days_old,
        )

    def _prepare_standalone_data(
        self, results: Dict, id_to_name: Dict,
        title_info: Optional[Dict] = None, rss_items: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        return inputs.prepare_standalone_data(
            results, id_to_name,
            standalone_config=self.ctx.config.get("DISPLAY", {}).get("STANDALONE", {}),
            title_info=title_info, rss_items=rss_items,
        )

    def prepare_report(self, crawl: CrawlResult, rss_collection: RSSCollection) -> PreparedReportInput:
        """冻结来源数据，确定本轮标题归类。"""
        feed_ids = [feed["id"] for feed in self.ctx.rss_feeds
                    if feed.get("enabled", True)] if self.ctx.rss_enabled else []
        captured = capture_sources(
            self.ctx.create_daily_source_reader(), self._publication_beacon().load(),
            self.ctx.get_time(), platform_ids=self.ctx.platform_ids, feed_ids=feed_ids,
            rss_available=rss_collection.available, news_available=not bool(crawl.failed_ids),
        )
        frozen = captured.to_dict()
        filter_enabled = self.ctx.config.get("AI_FILTER", {}).get("ENABLED", False)
        keywords = (KeywordRules([], [], []) if filter_enabled else
                    KeywordRules(*deepcopy(self.ctx.load_frequency_words(self.frequency_file))))
        hotlist = inputs.prepare_captured_hotlist(frozen, self.report_mode)
        rss_result = self._process_rss_data_by_mode(frozen, keywords, defer_grouping=True)
        from .filtering import candidate_titles, keyword_groups
        titles = candidate_titles(hotlist, rss_result, mode=self.report_mode,
                                  include_rss=self.ctx.config.get("DISPLAY", {}).get("REGIONS", {}).get("RSS", True))
        filter_fallback = False
        if filter_enabled:
            from trendradar.ai.filter import AIFilter, FilterError
            try:
                selection = AIFilter(self.ctx.config["AI_FILTER"]).classify(titles)
                keywords = KeywordRules(selection.word_groups, [], [])
                title_groups = {title: selection.group_for(title) for title in titles}
            except FilterError as error:
                print(f"[筛选] {error}；本轮整体回退本地关键词筛选")
                keywords = KeywordRules(*deepcopy(self.ctx.load_frequency_words(self.frequency_file)))
                title_groups = keyword_groups(titles, keywords)
                filter_fallback = True
        else:
            title_groups = keyword_groups(titles, keywords)
        rss_result = rss.group_rss_input(
            rss_result, mode=self.report_mode, config=self.ctx.config, keywords=keywords,
            timezone=self.ctx.timezone, rank_threshold=self.rank_threshold,
            group_resolver=title_groups.__getitem__,
        )
        standalone = self._prepare_standalone_data(
            hotlist.results, hotlist.id_to_name, hotlist.title_info, rss_result.raw_items,
        )
        return PreparedReportInput(
            mode=self.report_mode, hotlist=hotlist, keywords=keywords, title_groups=title_groups,
            rss=rss_result, failed_ids=crawl.failed_ids, standalone=standalone,
            capture=captured, filter_fallback=filter_fallback,
        )

    def analyze_report(
        self, prepared: PreparedReportInput, schedule: ResolvedSchedule = None,
    ) -> ReportArtifacts:
        """生成展示名单、分析正文和HTML。"""
        hotlist, keywords, rss_result = prepared.hotlist, prepared.keywords, prepared.rss
        stats, _ = self.ctx.count_frequency(
            hotlist.results, keywords.word_groups, keywords.filter_words,
            hotlist.id_to_name, hotlist.title_info, hotlist.new_titles,
            mode=prepared.mode, global_filters=keywords.global_filters, quiet=prepared.quiet,
            group_resolver=prepared.title_groups.__getitem__,
        )
        if self.ctx.display_mode == "platform" and stats:
            stats = convert_keyword_stats_to_platform_stats(
                stats, self.ctx.weight_config, self.ctx.rank_threshold,
            )
        from .filtering import prepare_analysis_stats, prepare_visible_report
        report_data, has_visible_news = prepare_visible_report(self.ctx, prepared, stats)
        analysis_stats = prepare_analysis_stats(prepared, report_data, stats)

        ai_result = None
        if self.ctx.config.get("AI_ANALYSIS", {}).get("ENABLED", False) and any(
            stat.get("titles") for stat in analysis_stats + (rss_result.stats or [])
        ):
            ai_result = self._run_ai_analysis(
                analysis_stats, rss_result.stats, prepared.mode,
                self._get_mode_strategy()["report_type"], schedule=schedule,
            )
        html_file = None
        if self.ctx.config["STORAGE"]["FORMATS"]["HTML"]:
            html_file = self.ctx.generate_html(
                report_data, mode=prepared.mode,
                rss_items=rss_result.stats, rss_new_items=rss_result.new_stats,
                ai_analysis=ai_result, standalone_data=prepared.standalone,
                captured_at=prepared.capture.to_dict()["captured_at"],
            )
        return ReportArtifacts(stats, html_file, has_visible_news)

    def execute_report(
        self, crawl: CrawlResult, rss_result: RSSCollection, schedule: ResolvedSchedule = None,
        *, notify=False,
    ) -> Optional[str]:
        """Analyze and render one captured input."""
        prepared = self.prepare_report(crawl, rss_result)
        artifacts = self.analyze_report(prepared, schedule)
        html_file = artifacts.html_file
        if html_file:
            print(f"邮箱HTML报告已生成: {html_file}")
        if notify:
            self._send_notification_if_needed(prepared, artifacts, schedule)
        if self._should_open_browser() and html_file:
            webbrowser.open("file://" + str(Path(html_file).resolve()))
        return html_file

    def run(self) -> None:
        """Collect first; sending never controls the collection schedule."""
        try:
            if not self._initialize_and_check_config():
                return
            schedule = self._resolve_schedule()
            if not schedule.collect or not self.ctx.config["ENABLE_CRAWLER"]:
                return
            crawl = self._crawl_data()
            rss_result = self._crawl_rss_data()
            notify = (self.ctx.config.get("ENABLE_NOTIFICATION", False)
                      and self._has_notification_configured()
                      and (schedule.push or self._manual_force_run()))
            if not notify:
                print("[采集] 非推送轮次：仅采集入库，不筛选、不分析、不生成报告")
                return
            if not self._action_allowed(schedule, "push"):
                return
            self.execute_report(crawl, rss_result, schedule, notify=True)
        except Exception as exc:
            print(f"分析流程停止（{type(exc).__name__}）")
            raise
        finally:
            self.ctx.cleanup()
