# coding=utf-8
"""Daily application runner: order, action gates and resource lifetime."""

import os
import webbrowser
from functools import partial
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
from .models import CrawlResult, KeywordRules, ModeInput, PreparedReportInput, ReportArtifacts, RSSResult


class DailyRunner:
    """Coordinate explicit collection/input steps, then AI, HTML and delivery."""

    MODE_STRATEGIES = {
        "incremental": {
            "mode_name": "增量模式",
            "description": "增量模式（只关注新增新闻，无新增时不推送）",
            "report_type": "增量分析",
            "should_send_notification": True,
        },
        "current": {
            "mode_name": "当前榜单模式",
            "description": "当前榜单模式（当前榜单匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "当前榜单",
            "should_send_notification": True,
        },
        "daily": {
            "mode_name": "全天汇总模式",
            "description": "全天汇总模式（所有匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "全天汇总",
            "should_send_notification": True,
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
        self.update_info = None
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

    def _has_valid_content(
        self, stats: List[Dict], new_titles: Optional[Dict] = None
    ) -> bool:
        """检查是否有有效的新闻内容"""
        if self.report_mode == "incremental":
            # 增量模式：只要有匹配的新闻就推送
            # count_word_frequency 已经确保只处理新增的新闻（包括当天第一次爬取的情况）
            has_matched_news = any(stat["count"] > 0 for stat in stats)
            return has_matched_news
        elif self.report_mode == "current":
            # current模式：只要stats有内容就说明有匹配的新闻
            return any(stat["count"] > 0 for stat in stats)
        else:
            # 当日汇总模式下，检查是否有匹配的频率词新闻或新增新闻
            has_matched_news = any(stat["count"] > 0 for stat in stats)
            has_new_news = bool(
                new_titles and any(len(titles) > 0 for titles in new_titles.values())
            )
            return has_matched_news or has_new_news

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
            scheduler = self.ctx.create_scheduler()
            date_str = self.ctx.format_date()
            already_executed = scheduler.already_executed(schedule.period_key, action, date_str)
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
        id_to_name: Optional[Dict],
        schedule: ResolvedSchedule = None,
        standalone_data: Optional[Dict] = None,
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

            # AI 始终消费本次报告模式已经准备好的统一新闻候选池。
            source_names = set(id_to_name.values()) if id_to_name else set()
            for stat in rss_items or []:
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
                standalone_data=standalone_data,
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

    def _send_notification_if_needed(
        self,
        stats: List[Dict],
        report_type: str,
        new_titles: Optional[Dict] = None,
        html_file_path: Optional[str] = None,
        rss_items: Optional[List[Dict]] = None,
        schedule: ResolvedSchedule = None,
    ) -> bool:
        """判断是否需要发送，并投递已生成的邮件 HTML。"""
        has_notification = self._has_notification_configured()
        cfg = self.ctx.config

        # 检查是否有有效内容（热榜或RSS）
        has_news_content = self._has_valid_content(stats, new_titles)
        has_rss_content = bool(rss_items and len(rss_items) > 0)
        has_any_content = has_news_content or has_rss_content

        # 计算热榜匹配条数
        news_count = sum(len(stat.get("titles", [])) for stat in stats) if stats else 0
        rss_count = sum(stat.get("count", 0) for stat in rss_items) if rss_items else 0

        if (
            cfg["ENABLE_NOTIFICATION"]
            and has_notification
            and has_any_content
        ):
            # 输出推送内容统计
            content_parts = []
            if news_count > 0:
                content_parts.append(f"热榜 {news_count} 条")
            if rss_count > 0:
                content_parts.append(f"RSS {rss_count} 条")
            total_count = news_count + rss_count
            print(f"[推送] 准备发送：{' + '.join(content_parts)}，合计 {total_count} 条")

            if not self._action_allowed(schedule, "push"):
                return False

            # 邮件 HTML 已在分析流水线中生成，这里仅发送明确文件路径。
            # 发件人/主题优先使用时段名：早间速览/午间速览/傍晚速览/全天汇总
            dispatcher = self.ctx.create_notification_dispatcher()
            result = dispatcher.send_report(
                report_type=report_type,
                html_file_path=html_file_path,
                period_name=(schedule.period_name if schedule else None),
            )

            if not result.configured:
                print("未配置任何通知渠道，跳过通知发送")
                return False

            # 部分接受也占用一次推送窗口，防止下轮整批补发；仍返回未全成功。
            if result.sent or result.partially_delivered:
                if schedule.once_push and schedule.period_key:
                    scheduler = self.ctx.create_scheduler()
                    date_str = self.ctx.format_date()
                    scheduler.record_execution(schedule.period_key, "push", date_str)

            return result.sent

        elif cfg["ENABLE_NOTIFICATION"] and not has_notification:
            print("⚠️ 警告：通知功能已启用但未配置任何通知渠道，将跳过通知发送")
        elif not cfg["ENABLE_NOTIFICATION"]:
            print(f"跳过{report_type}通知：通知功能已禁用")
        elif (
            cfg["ENABLE_NOTIFICATION"]
            and has_notification
            and not has_any_content
        ):
            mode_strategy = self._get_mode_strategy()
            if self.report_mode == "incremental":
                if not has_rss_content:
                    print("跳过通知：增量模式下未检测到匹配的新闻和RSS")
                else:
                    print("跳过通知：增量模式下新闻未匹配到关键词")
            else:
                print(
                    f"跳过通知：{mode_strategy['mode_name']}下未检测到匹配的新闻"
                )

        return False

    def _initialize_and_check_config(self) -> bool:
        """通用初始化和配置检查"""
        now = self.ctx.get_time()
        print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if not self.ctx.config["ENABLE_CRAWLER"]:
            print("爬虫功能已禁用（ENABLE_CRAWLER=False），程序退出")
            return False

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

    def _crawl_rss_data(self) -> RSSResult:
        return collection.crawl_rss(
            enabled=self.ctx.rss_enabled, get_feeds=lambda: self.ctx.rss_feeds,
            create_fetcher=self._create_rss_fetcher,
            save_rss_data=lambda data: self.storage_manager.save_rss_data(data),
            prepare_input=self._process_rss_data_by_mode,
        )

    def _process_rss_data_by_mode(self, rss_data) -> RSSResult:
        return rss.prepare_rss_input(
            rss_data, mode=self.report_mode, storage=self.storage_manager,
            config=self.ctx.config,
            load_frequency_words=partial(self.ctx.load_frequency_words, self.frequency_file),
            convert_items=self._convert_rss_items_to_list,
            timezone=self.ctx.timezone, rank_threshold=self.rank_threshold,
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

    def _load_analysis_data(self, quiet: bool = False):
        # Defer context access to the loader's original exception boundary.
        return inputs.load_analysis_data(
            lambda: self.ctx.platform_ids,
            read_today_titles=lambda *args, **kw: self.ctx.read_today_titles(*args, **kw),
            detect_new_titles=lambda *args, **kw: self.ctx.detect_new_titles(*args, **kw),
            load_frequency_words=lambda: self.ctx.load_frequency_words(self.frequency_file),
            quiet=quiet,
        )

    _prepare_current_title_info = staticmethod(inputs.prepare_current_title_info)

    def _select_mode_data(
        self, results: Dict, id_to_name: Dict, new_titles: Dict, time_info: str,
    ) -> ModeInput:
        return inputs.select_mode_data(
            self.report_mode, results, id_to_name, new_titles, time_info,
            load_history=self._load_analysis_data,
            prepare_current_info=self._prepare_current_title_info,
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

    def prepare_report(self, crawl: CrawlResult, rss_result: RSSResult) -> PreparedReportInput:
        """Prepare each mode's hotlist and raw standalone views before analysis."""
        new_titles = self.ctx.detect_new_titles(self.ctx.platform_ids)
        time_info = self.ctx.format_time()
        keywords = KeywordRules(*self.ctx.load_frequency_words(self.frequency_file))
        hotlist = self._select_mode_data(
            crawl.results, crawl.id_to_name, new_titles, time_info,
        )
        standalone = self._prepare_standalone_data(
            hotlist.results, hotlist.id_to_name, hotlist.title_info, rss_result.raw_items,
        )
        return PreparedReportInput(
            mode=self.report_mode, hotlist=hotlist, keywords=keywords,
            rss=rss_result, failed_ids=crawl.failed_ids, standalone=standalone,
        )

    def analyze_report(
        self, prepared: PreparedReportInput, schedule: ResolvedSchedule = None,
    ) -> ReportArtifacts:
        """Keyword statistics → AI → HTML, with one explicit prepared input."""
        hotlist, keywords, rss_result = prepared.hotlist, prepared.keywords, prepared.rss
        stats, _ = self.ctx.count_frequency(
            hotlist.results, keywords.word_groups, keywords.filter_words,
            hotlist.id_to_name, hotlist.title_info, hotlist.new_titles,
            mode=prepared.mode, global_filters=keywords.global_filters, quiet=prepared.quiet,
        )
        if self.ctx.display_mode == "platform" and stats:
            stats = convert_keyword_stats_to_platform_stats(
                stats, self.ctx.weight_config, self.ctx.rank_threshold,
            )

        # AI consumes the same keyword-filtered hotlist/RSS pool as the report.
        # Standalone remains a separate input; raw RSS never enlarges this pool.
        ai_result = None
        ai_config = self.ctx.config.get("AI_ANALYSIS", {})
        if ai_config.get("ENABLED", False) and (stats or rss_result.stats):
            report_type = self._get_mode_strategy()["report_type"]
            ai_result = self._run_ai_analysis(
                stats, rss_result.stats, prepared.mode, report_type, hotlist.id_to_name,
                schedule=schedule, standalone_data=prepared.standalone,
            )

        html_file = None
        if self.ctx.config["STORAGE"]["FORMATS"]["HTML"]:
            html_file = self.ctx.generate_html(
                stats, failed_ids=prepared.failed_ids, new_titles=hotlist.new_titles,
                id_to_name=hotlist.id_to_name, mode=prepared.mode,
                rss_items=rss_result.stats, rss_new_items=rss_result.new_stats,
                ai_analysis=ai_result, standalone_data=prepared.standalone,
                frequency_file=self.frequency_file,
            )
        return ReportArtifacts(stats, html_file)

    def execute_report(
        self, crawl: CrawlResult, rss_result: RSSResult, schedule: ResolvedSchedule = None,
    ) -> Optional[str]:
        """Prepare the scheduled report, analyze/render it, then send and display."""
        mode_strategy = self._get_mode_strategy()
        prepared = self.prepare_report(crawl, rss_result)
        artifacts = self.analyze_report(prepared, schedule)
        html_file = artifacts.html_file
        if html_file:
            print(f"邮箱HTML报告已生成: {html_file}")
            print(f"最新邮箱报告已更新: output/html/latest/{self.report_mode}.html")

        if mode_strategy["should_send_notification"]:
            self._send_notification_if_needed(
                artifacts.stats, mode_strategy["report_type"],
                new_titles=prepared.hotlist.new_titles, html_file_path=html_file,
                rss_items=rss_result.stats, schedule=schedule,
            )

        if self._should_open_browser() and html_file:
            file_url = "file://" + str(Path(html_file).resolve())
            print(f"正在打开邮箱HTML报告: {file_url}")
            webbrowser.open(file_url)
        elif self.is_docker_container and html_file:
            print(f"邮箱HTML报告已生成（Docker环境）: {html_file}")
        return html_file

    def run(self) -> None:
        """Resolve → hotlist → RSS → prepare/AI/HTML/email → cleanup."""
        try:
            if not self._initialize_and_check_config():
                return
            schedule = self._resolve_schedule()
            if not schedule.collect:
                print("[调度] 当前时间段不执行数据采集，跳过分析流水线")
                return
            crawl = self._crawl_data()
            rss_result = self._crawl_rss_data()
            self.execute_report(crawl, rss_result, schedule)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"分析流程执行出错: {e}")
            raise
        finally:
            self.ctx.cleanup()
