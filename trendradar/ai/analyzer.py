# coding=utf-8
"""
AI 分析器模块

调用 AI 大模型对热点新闻进行深度分析
通过 AIClient（LiteLLM）调用模型
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from trendradar.ai.client import AIClient
from trendradar.ai.selector import SelectedNewsCluster, select_ai_news


@dataclass
class AIAnalysisSection:
    """一个经过契约校验的 AI 分析模块。"""
    title: str
    content: str
    render_style: str = "prose"


@dataclass(frozen=True)
class AISectionSpec:
    """AI 输出模块的结构与渲染契约。"""
    title: str
    required: bool = False
    content_type: str = "prose"
    render_style: str = "prose"


AI_SECTION_PRESENCE = {"required", "optional"}
AI_SECTION_CONTENT_TYPES = {"lead_and_events", "bullet_list", "prose"}
AI_SECTION_RENDER_STYLES = {"numbered_subtitles", "ordered_bullets", "prose"}


@dataclass
class AIAnalysisResult:
    """AI 分析结果"""
    sections: List[AIAnalysisSection] = field(default_factory=list)

    # 基础元数据
    raw_response: str = ""               # 原始响应
    success: bool = False                # 是否成功
    error: str = ""                      # 错误信息
    model: str = ""                      # 本次实际调用成功的模型名

    # 新闻数量统计
    total_news: int = 0                  # 关键词命中标题数
    analyzed_news: int = 0               # 事件簇数
    max_news_limit: int = 0              # 事件簇上限


class AIAnalyzer:
    """AI 分析器"""

    def __init__(
        self,
        ai_config: Dict[str, Any],
        analysis_config: Dict[str, Any],
        get_time_func: Callable,
        debug: bool = False,
    ):
        """
        初始化 AI 分析器

        Args:
            ai_config: AI 模型配置（MODEL / API_KEY / FALLBACK_MODELS 等）
            analysis_config: AI 分析功能配置（language, prompt_file 等）
            get_time_func: 获取当前时间的函数
            debug: 是否开启调试模式
        """
        self.ai_config = ai_config
        self.analysis_config = analysis_config
        self.get_time_func = get_time_func
        self.debug = debug

        # 创建统一 AI 客户端（LiteLLM）
        self.client = AIClient(ai_config)

        # 验证配置
        valid, error = self.client.validate_config()
        if not valid:
            print(f"[AI] 配置警告: {error}")

        # 从分析配置获取功能参数
        self.max_news = analysis_config.get("MAX_EVENTS_FOR_ANALYSIS", 120)
        self.include_rank_timeline = analysis_config.get("INCLUDE_RANK_TIMELINE", False)
        self.language = analysis_config.get("LANGUAGE", "Chinese")

        # 加载提示词模板及其输出模块契约
        (
            self.system_prompt,
            self.user_prompt_template,
            self.section_specs,
        ) = self._load_prompt_template(
            analysis_config.get("PROMPT_FILE", "ai_analysis_prompt.txt")
        )

    def _load_prompt_template(self, prompt_file: str) -> tuple:
        """加载提示词模板及文件头中的模块契约。"""
        config_dir = Path(__file__).parent.parent.parent / "config"
        prompt_path = config_dir / prompt_file

        if not prompt_path.exists():
            raise FileNotFoundError(f"AI 提示词文件不存在: {prompt_path}")

        content = prompt_path.read_text(encoding="utf-8")
        section_specs = self._parse_section_specs(content)

        # 解析 [system] 和 [user] 部分
        system_prompt = ""
        user_prompt = ""

        if "[system]" in content and "[user]" in content:
            parts = content.split("[user]")
            system_part = parts[0]
            user_part = parts[1] if len(parts) > 1 else ""

            # 提取 system 内容
            if "[system]" in system_part:
                system_prompt = system_part.split("[system]")[1].strip()

            user_prompt = user_part.strip()
        else:
            # 整个文件作为 user prompt
            user_prompt = content

        template_titles = re.findall(r"(?m)^## ([^\r\n]+)$", user_prompt)
        spec_titles = [spec.title for spec in section_specs]
        if template_titles != spec_titles:
            raise ValueError(
                "AI 模块契约与提示词二级标题不一致："
                f"契约={spec_titles}，提示词={template_titles}"
            )

        return system_prompt, user_prompt, section_specs

    @staticmethod
    def _parse_section_specs(content: str) -> tuple:
        """从提示词头部读取并校验 `# AI_SECTION:` 模块契约。"""
        directive_lines = re.findall(r"(?m)^# AI_SECTION:.*$", content)
        if not directive_lines:
            raise ValueError("AI 提示词缺少 # AI_SECTION 模块契约")

        pattern = re.compile(
            r"^# AI_SECTION:\s*([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\s*$"
        )
        specs = []
        for line in directive_lines:
            match = pattern.match(line)
            if not match:
                raise ValueError(f"AI 模块契约格式错误：{line}")
            title, presence, content_type, render_style = (
                value.strip() for value in match.groups()
            )
            if presence not in AI_SECTION_PRESENCE:
                raise ValueError(f"AI 模块 {title} 的必选性无效：{presence}")
            if content_type not in AI_SECTION_CONTENT_TYPES:
                raise ValueError(f"AI 模块 {title} 的内容类型无效：{content_type}")
            if render_style not in AI_SECTION_RENDER_STYLES:
                raise ValueError(f"AI 模块 {title} 的渲染样式无效：{render_style}")
            specs.append(
                AISectionSpec(
                    title=title,
                    required=presence == "required",
                    content_type=content_type,
                    render_style=render_style,
                )
            )

        titles = [spec.title for spec in specs]
        duplicate_titles = sorted({title for title in titles if titles.count(title) > 1})
        if duplicate_titles:
            raise ValueError(f"AI 模块契约包含重复标题：{'、'.join(duplicate_titles)}")
        if not any(spec.required for spec in specs):
            raise ValueError("AI 模块契约至少需要一个必选模块")
        return tuple(specs)

    def analyze(
        self,
        stats: List[Dict],
        rss_stats: Optional[List[Dict]] = None,
        report_mode: str = "daily",
        report_type: str = "当日汇总",
        platforms: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
    ) -> AIAnalysisResult:
        """
        执行 AI 分析

        Args:
            stats: 热榜统计数据
            rss_stats: RSS 统计数据
            report_mode: 报告模式
            report_type: 报告类型
            platforms: 平台列表
            keywords: 关键词列表

        Returns:
            AIAnalysisResult: 分析结果
        """

        # 打印配置信息方便调试（不输出 API Key 任何字符）
        model = self.ai_config.get("MODEL", "unknown")
        api_base = self.ai_config.get("API_BASE", "")
        model_display = model.replace("/", "/\u200b") if model else "unknown"

        print(f"[AI] 模型: {model_display}")
        print(f"[AI] Key : {'已配置' if self.client.api_key else '未配置'}")

        if api_base:
            print(f"[AI] 接口: 存在自定义 API 端点")

        timeout = self.ai_config.get("TIMEOUT", self.client.timeout)
        print(f"[AI] 参数: timeout={timeout}")

        if not self.client.api_key:
            return AIAnalysisResult(
                success=False,
                error="未配置 AI API Key，请在 config.yaml 或环境变量 AI_API_KEY 中设置"
            )

        # 准备新闻内容并获取统计数据
        (
            news_content,
            total_news,
            analyzed_count,
        ) = self._prepare_news_content(stats, rss_stats)

        if not news_content:
            return AIAnalysisResult(
                success=False,
                error="没有可分析的新闻内容",
                total_news=total_news,
                analyzed_news=0,
                max_news_limit=self.max_news
            )

        # 构建提示词
        current_time = self.get_time_func().strftime("%Y-%m-%d %H:%M:%S")

        # 提取关键词
        if not keywords:
            keywords = [s.get("word", "") for s in stats if s.get("word")] if stats else []

        # 使用安全的字符串替换，避免模板中其他花括号（如 JSON 示例）被误解析
        user_prompt = self.user_prompt_template
        user_prompt = user_prompt.replace("{report_mode}", report_mode)
        user_prompt = user_prompt.replace("{report_type}", report_type)
        user_prompt = user_prompt.replace("{current_time}", current_time)
        user_prompt = user_prompt.replace(
            "{news_count}", str(analyzed_count)
        )
        user_prompt = user_prompt.replace("{platforms}", ", ".join(platforms) if platforms else "多平台")
        user_prompt = user_prompt.replace("{keywords}", ", ".join(keywords[:20]) if keywords else "无")
        user_prompt = user_prompt.replace("{news_content}", news_content)
        user_prompt = user_prompt.replace("{language}", self.language)

        if self.debug:
            print("\n" + "=" * 80)
            print("[AI 调试] 发送给 AI 的完整提示词")
            print("=" * 80)
            if self.system_prompt:
                print("\n--- System Prompt ---")
                print(self.system_prompt)
            print("\n--- User Prompt ---")
            print(user_prompt)
            print("=" * 80 + "\n")

        # 调用 AI API
        try:
            response = self._call_ai(user_prompt)
            if self._last_ai_call_was_truncated():
                reason = getattr(self.client, "last_finish_reason", None) or "length"
                return AIAnalysisResult(
                    success=False,
                    error=(
                        f"AI 输出被截断（finish_reason={reason}），为避免展示不完整内容，本轮不使用该结果。"
                        "请压缩 AI 分析提示词或换用支持更长输出的模型。"
                    ),
                    total_news=total_news,
                    analyzed_news=analyzed_count,
                    max_news_limit=self.max_news,
                )

            # 记录正文分析实际模型
            used_model = getattr(self.client, "last_model", None) or ""

            result = self._parse_response(response)

            # 填充统计数据
            result.total_news = total_news
            result.analyzed_news = analyzed_count
            result.max_news_limit = self.max_news
            result.model = used_model
            return result
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)

            # 截断过长的错误消息
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            friendly_msg = f"AI 分析失败 ({error_type}): {error_msg}"

            return AIAnalysisResult(
                success=False,
                error=friendly_msg
            )

    def _format_item_input_line(self, item: Dict[str, Any]) -> str:
        """将事件簇中的一条证据标题格式化为内部 AI 输入。"""
        title = str(item.get("title", "") or "").strip()
        source = item.get("source_name", item.get("feed_name", item.get("source", "")))
        line = f"- [{source}] {title}" if source else f"- {title}"
        ranks = [rank for rank in self._rank_values(item) if rank > 0]
        if ranks:
            low, high = min(ranks), max(ranks)
            line += f" | 排名:{low if low == high else f'{low}-{high}'}"
        time_display = item.get("time_display", "")
        if time_display:
            line += f" | 时间:{time_display}"
        else:
            line += f" | 时间:{self._format_time_range(item.get('first_time', ''), item.get('last_time', ''))}"
        if item.get("count"):
            line += f" | 出现:{item['count']}次"
        if self.include_rank_timeline:
            line += f" | 轨迹:{self._format_rank_timeline(item.get('rank_timeline', []))}"
        return line

    @staticmethod
    def _rank_values(item: Dict[str, Any]) -> List[int]:
        values: List[int] = []
        for point in item.get("rank_timeline", []) or []:
            rank = point.get("rank") if isinstance(point, dict) else point
            try:
                values.append(int(rank))
            except (TypeError, ValueError):
                continue
        if values:
            return values
        for rank in item.get("ranks", []) or []:
            try:
                values.append(int(rank))
            except (TypeError, ValueError):
                continue
        return values

    def _format_cluster(self, index: int, cluster: SelectedNewsCluster) -> str:
        representative = cluster.representative_item
        title = str(representative.get("title", "") or "").strip()
        lines = [
            f"### 事件 {index}：{title}",
            f"- 来源覆盖：{len(cluster.sources)} | 相关标题：{len(cluster.member_items)}",
        ]
        seen = set()
        # 每个事件只保留少量不同措辞证据，避免热门事件挤占整份提示词。
        for item in cluster.member_items[:6]:
            item_title = str(item.get("title", "") or "").strip()
            source = str(item.get("source_name", item.get("feed_name", item.get("source", ""))) or "")
            key = (source, item_title)
            if not item_title or key in seen:
                continue
            seen.add(key)
            lines.append(self._format_item_input_line(item))
        return "\n".join(lines)

    def _prepare_news_content(
        self,
        stats: List[Dict],
        rss_stats: Optional[List[Dict]] = None,
    ) -> tuple:
        """将关键词命中标题保守聚成事件簇，再准备 AI 输入。"""
        total_news = sum(len(stat.get("titles", [])) for stat in (stats or []))
        total_news += sum(len(stat.get("titles", [])) for stat in (rss_stats or []))
        selection = select_ai_news(
            stats=stats,
            rss_stats=rss_stats,
            total_limit=self.max_news,
        )
        event_content = "\n\n".join(
            self._format_cluster(index, cluster)
            for index, cluster in enumerate(selection.clusters, 1)
        )
        print(
            f"[AI] 关键词命中标题 {total_news} 条；聚簇后输入 "
            f"{selection.selected_count} 个事件（事件上限={self.max_news}）"
        )
        return (
            event_content,
            total_news,
            selection.selected_count,
        )

    def _call_ai(self, user_prompt: str) -> str:
        """调用 AI API"""
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        kwargs = {}
        timeout = self.ai_config.get("TIMEOUT")
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self.client.chat(messages, **kwargs)

    def _last_ai_call_was_truncated(self) -> bool:
        """判断上一次模型调用是否因输出长度限制被截断。"""
        reason = str(getattr(self.client, "last_finish_reason", "") or "").lower()
        if not reason:
            return False
        return reason in {"length", "max_tokens", "token_limit"} or "length" in reason or "max_tokens" in reason

    def _format_time_range(self, first_time: str, last_time: str) -> str:
        """格式化时间范围（简化显示，只保留时分）"""
        def extract_time(time_str: str) -> str:
            if not time_str:
                return "-"
            # 尝试提取 HH:MM 部分
            if " " in time_str:
                parts = time_str.split(" ")
                if len(parts) >= 2:
                    time_part = parts[1]
                    if ":" in time_part:
                        return time_part[:5]  # HH:MM
            elif ":" in time_str:
                return time_str[:5]
            # 处理 HH-MM 格式
            result = time_str[:5] if len(time_str) >= 5 else time_str
            if len(result) == 5 and result[2] == '-':
                result = result.replace('-', ':')
            return result

        first = extract_time(first_time)
        last = extract_time(last_time)

        if first == last or last == "-":
            return first
        return f"{first}~{last}"

    def _format_rank_timeline(self, rank_timeline: List[Dict]) -> str:
        """格式化排名时间线"""
        if not rank_timeline:
            return "-"

        parts = []
        for item in rank_timeline:
            time_str = item.get("time", "")
            if len(time_str) == 5 and time_str[2] == '-':
                time_str = time_str.replace('-', ':')
            rank = item.get("rank")
            if rank is None:
                parts.append(f"0({time_str})")
            else:
                parts.append(f"{rank}({time_str})")

        return "→".join(parts)

    @staticmethod
    def _strip_markdown_fences(response: str) -> str:
        return re.sub(
            r"(?im)^\s*```(?:markdown|md|text)?\s*$", "", response or ""
        ).strip()

    @staticmethod
    def _validate_lead_and_events(title: str, content: str) -> str:
        """校验“单句总领 + 三级事件标题 + 事件正文”模块。"""
        subtitles = list(re.finditer(r"(?m)^### ([^\r\n]+)$", content))
        if not subtitles:
            return f"{title}必须至少包含一个严格的三级事件标题"
        subtitle_names = [match.group(1) for match in subtitles]
        if len(set(subtitle_names)) != len(subtitle_names):
            return f"{title}包含重复的三级事件标题"
        lead = content[:subtitles[0].start()].strip()
        if not lead:
            return f"{title}必须在首个事件标题前包含一句总领"
        if "\n" in lead or re.match(r"^(?:[-*]|\d+[.、])\s+", lead):
            return f"{title}总领必须是单独一个自然段"
        sentence_ends = re.findall(r"[。！？]", lead)
        if len(sentence_ends) != 1 or not re.search(r"[。！？][”’」』】]?$", lead):
            return f"{title}总领必须只写一句话并以句号、问号或感叹号结尾"
        for index, subtitle in enumerate(subtitles):
            end = (
                subtitles[index + 1].start()
                if index + 1 < len(subtitles)
                else len(content)
            )
            if not content[subtitle.end():end].strip():
                return f"{title}事件缺少正文：{subtitle.group(1)}"
        return ""

    def _parse_response(self, response: str) -> AIAnalysisResult:
        """严格解析事件中心 Markdown；JSON、旧标题和变体均失败。"""
        if not response or not response.strip():
            return AIAnalysisResult(raw_response=response, error="AI 返回空响应")

        markdown = self._strip_markdown_fences(response)
        result = AIAnalysisResult(raw_response=response)
        specs = self.section_specs
        section_specs = {spec.title: spec for spec in specs}
        pattern = re.compile(
            r"(?m)^## (" + "|".join(map(re.escape, section_specs)) + r")$"
        )
        matches = list(pattern.finditer(markdown))
        if not matches:
            result.error = "未识别当前固定 Markdown 标题"
            return result

        all_h2_titles = re.findall(r"(?m)^##(?!#)(?:\s+)?([^\r\n]*)$", markdown)
        invalid_titles = [title for title in all_h2_titles if title not in section_specs]
        matched_titles = [match.group(1) for match in matches]
        duplicate_titles = sorted({t for t in matched_titles if matched_titles.count(t) > 1})
        missing_titles = [
            spec.title
            for spec in specs
            if spec.required and spec.title not in matched_titles
        ]
        expected_titles = [
            spec.title for spec in specs if spec.title in matched_titles
        ]
        wrong_order = not duplicate_titles and matched_titles != expected_titles
        prefix = markdown[: matches[0].start()].strip()
        if invalid_titles or duplicate_titles or missing_titles or wrong_order or prefix:
            problems = []
            if invalid_titles:
                problems.append(f"未知标题：{'、'.join(invalid_titles)}")
            if duplicate_titles:
                problems.append(f"重复标题：{'、'.join(duplicate_titles)}")
            if missing_titles:
                problems.append(f"缺少标题：{'、'.join(missing_titles)}")
            if wrong_order:
                problems.append("标题顺序与当前固定结构不一致")
            if prefix:
                problems.append("首个固定标题前存在正文")
            result.error = "Markdown 固定标题校验失败（" + "；".join(problems) + "）"
            return result

        contents: Dict[str, str] = {}
        for index, match in enumerate(matches):
            title = match.group(1)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            content = markdown[match.end():end].strip()
            if not content:
                result.error = f"Markdown 模块内容为空：{title}"
                return result
            contents[title] = content

        for spec in specs:
            content = contents.get(spec.title, "")
            if not content:
                continue
            if spec.content_type == "lead_and_events":
                validation_error = self._validate_lead_and_events(spec.title, content)
                if validation_error:
                    result.error = validation_error
                    return result
            elif spec.content_type == "bullet_list" and any(
                line.strip() and not re.match(r"^-\s+\S", line.strip())
                for line in content.splitlines()
            ):
                result.error = f"{spec.title}必须只包含无序列表项"
                return result

        result.sections = [
            AIAnalysisSection(
                title=spec.title,
                content=contents[spec.title],
                render_style=spec.render_style,
            )
            for spec in specs
            if spec.title in contents
        ]
        result.success = True
        return result
