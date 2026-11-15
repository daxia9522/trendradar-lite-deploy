# coding=utf-8
"""AI 分析器模块"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from trendradar.ai.client import AIClient


@dataclass
class AIAnalysisSection:
    """一个经过契约校验的 AI 分析模块。"""
    title: str
    content: str
    format_type: str = "prose"


@dataclass(frozen=True)
class AISectionSpec:
    """AI 输出模块的结构与渲染契约。"""
    title: str
    required: bool = False
    format_type: str = "prose"


AI_SECTION_PRESENCE = {"required", "optional"}
AI_SECTION_FORMATS = {"events", "bullets", "lead_points", "prose"}

# 分条序号的基数：阿拉伯数字、中文数字、单个拉丁字母。
_POINT_ORDINAL = r"(?:\d{1,3}|[一二三四五六七八九十百]{1,4}|[A-Za-z])"

# 可辨识的分条标记。
_POINT_MARKER = re.compile(
    r"^(?:"
    r"#{3,6}\s*\S"
    r"|[-*+•·・‣▪▫●○◆■□]\s*\S"
    r"|[\u2460-\u2473\u2488-\u249b]\s*\S"
    rf"|[(（\[【]\s*{_POINT_ORDINAL}\s*[)）\]】]\s*\S"
    rf"|{_POINT_ORDINAL}\s*[.．、,，)）:：]\s*\S"
    r"|(?:\*\*|__)\s*\S"
    r")"
)

# 列表项不能充当总括句。
_LEAD_AS_POINT = re.compile(
    r"^(?:"
    r"#{1,6}\s"
    r"|[-*+•·・‣▪▫●○◆■□]\s"
    r"|[\u2460-\u2473\u2488-\u249b]\s*\S"
    rf"|[(（\[【]\s*{_POINT_ORDINAL}\s*[)）\]】]"
    rf"|{_POINT_ORDINAL}\s*[.．、)）](?:\s|\*\*|__)"
    r")"
)


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
    total_news: int = 0                  # 展示候选条数
    analyzed_news: int = 0               # 分析条数


class AIAnalyzer:
    """AI 分析器"""

    def __init__(
        self,
        ai_config: Dict[str, Any],
        analysis_config: Dict[str, Any],
        get_time_func: Callable,
        debug: bool = False,
    ):
        """初始化 AI 分析器"""
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

        required_structure = "\n\n".join(
            f"## {spec.title}" for spec in section_specs if spec.required
        )
        optional_structure = "\n\n".join(
            f"## {spec.title}" for spec in section_specs if not spec.required
        )
        output_contract = (
            "必须使用以下必选 Markdown 二级标题，并在每个标题下填写对应内容。"
            "标题不得修改、遗漏或重复：\n\n"
            f"{required_structure}"
        )
        if optional_structure:
            output_contract += (
                "\n\n以下是可选 Markdown 二级标题。仅在模板规定的条件成立时输出，"
                "否则省略；输出时标题不得修改或重复：\n\n"
                f"{optional_structure}"
            )
        user_prompt = (
            f"{user_prompt.rstrip()}\n\n"
            f"{output_contract}\n\n"
            "除上述标题及其正文外，不要输出 JSON、代码块、分析过程或其他内容。"
        )

        return system_prompt, user_prompt, section_specs

    @staticmethod
    def _parse_section_specs(content: str) -> tuple:
        """从提示词头部读取并校验 `# AI_SECTION:` 模块契约。"""
        directive_lines = re.findall(r"(?m)^# AI_SECTION:.*$", content)
        if not directive_lines:
            raise ValueError("AI 提示词缺少 # AI_SECTION 模块契约")

        pattern = re.compile(r"^# AI_SECTION:\s*([^|]+)\|([^|]+)\|([^|]+)\s*$")
        specs = []
        for line in directive_lines:
            match = pattern.match(line)
            if not match:
                raise ValueError(f"AI 模块契约格式错误：{line}")
            title, presence, format_type = (
                value.strip() for value in match.groups()
            )
            if presence not in AI_SECTION_PRESENCE:
                raise ValueError(f"AI 模块 {title} 的必选性无效：{presence}")
            if format_type not in AI_SECTION_FORMATS:
                raise ValueError(f"AI 模块 {title} 的格式无效：{format_type}")
            specs.append(
                AISectionSpec(
                    title=title,
                    required=presence == "required",
                    format_type=format_type,
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
        """执行 AI 分析"""

        # 打印配置信息方便调试（不输出 API Key 任何字符）
        model = self.ai_config.get("MODEL", "unknown")
        api_base = self.client.api_base
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
                error="未配置分析连接凭据，请检查 model_groups 中的凭据引用"
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
            )

        # 构建提示词
        current_time = self.get_time_func().strftime("%Y-%m-%d %H:%M:%S")

        # 提取关键词
        if keywords is None:
            keywords = [s.get("word", "") for s in stats if s.get("word")] if stats else []

        # system 与 user 均允许使用运行时变量；只替换已知占位符，保留其他花括号。
        prompt_values = {
            "report_mode": report_mode,
            "report_type": report_type,
            "current_time": current_time,
            "news_count": str(analyzed_count),
            "platforms": ", ".join(platforms) if platforms else "多平台",
            "keywords": ", ".join(keywords[:20]) if keywords else "无",
            "news_content": news_content,
            "language": self.language,
        }
        user_prompt = self._replace_prompt_variables(
            self.user_prompt_template, prompt_values
        )
        system_prompt = self._replace_prompt_variables(
            self.system_prompt, prompt_values
        )

        if self.debug:
            print("\n" + "=" * 80)
            print("[AI 调试] 发送给 AI 的完整提示词")
            print("=" * 80)
            if system_prompt:
                print("\n--- System Prompt ---")
                print(system_prompt)
            print("\n--- User Prompt ---")
            print(user_prompt)
            print("=" * 80 + "\n")

        # 调用 AI API
        try:
            result = self._generate_and_parse(user_prompt, system_prompt)

            # 填充统计数据
            result.total_news = total_news
            result.analyzed_news = analyzed_count
            return result
        except Exception as e:
            # Provider bodies can contain credential-bearing URLs or request details.
            # Do not return them to the report or the runner's logs.
            error_type = type(e).__name__
            status = getattr(e, "status_code", None)
            status_note = f", HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
            friendly_msg = f"AI 分析失败 ({error_type}{status_note})"

            return AIAnalysisResult(
                success=False,
                error=friendly_msg
            )

    def _format_item_input_line(self, item: Dict[str, Any]) -> str:
        """格式化标题及采集元数据。"""
        title = str(item.get("title", "") or "")
        source = item.get("source_name", item.get("feed_name", item.get("source", "")))
        line = f"- [{source}] {title}" if source else f"- {title}"
        ranks = [rank for rank in item.get("ranks", []) or [] if type(rank) is int and rank > 0]
        rank_range = "-"
        if ranks:
            low, high = min(ranks), max(ranks)
            rank_range = str(low) if low == high else f"{low}-{high}"
        time_range = item.get("time_display") or self._format_time_range(
            item.get("first_time", ""), item.get("last_time", ""),
        )
        line += f" | 排名:{rank_range} | 时间:{time_range} | 出现:{item.get('count', 1)}次"
        if self.include_rank_timeline:
            line += f" | 轨迹:{self._format_rank_timeline(item.get('rank_timeline', []))}"
        return line

    @staticmethod
    def _replace_prompt_variables(template: str, values: Dict[str, str]) -> str:
        """替换模板变量，保留数据中的花括号。"""
        pattern = r"\{" + "(?:" + "|".join(map(re.escape, values)) + ")" + r"\}"
        return re.sub(pattern, lambda match: values[match.group()[1:-1]], template or "")

    def _prepare_news_content(
        self, stats: List[Dict], rss_stats: Optional[List[Dict]] = None,
    ) -> tuple:
        """按栏目生成新闻文本。"""
        grouped = {}
        for group in (stats or []) + (rss_stats or []):
            grouped.setdefault(group["word"], []).extend(group.get("titles", []))
        lines = []
        total = 0
        for word, titles in grouped.items():
            if not titles:
                continue
            lines.append(f"\n**{word}** ({len(titles)}条)")
            lines.extend(self._format_item_input_line(item) for item in titles)
            total += len(titles)
        print(f"[AI] 分析材料 {total} 条，{sum(bool(items) for items in grouped.values())} 个栏目")
        return "\n".join(lines), total, total

    def _call_ai(self, user_prompt: str, system_prompt: Optional[str] = None) -> str:
        """调用 AI API"""
        messages = []
        effective_system_prompt = (
            self.system_prompt if system_prompt is None else system_prompt
        )
        if effective_system_prompt:
            messages.append({"role": "system", "content": effective_system_prompt})
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

    def _generate_and_parse(
        self, user_prompt: str, system_prompt: Optional[str] = None
    ) -> AIAnalysisResult:
        """生成并解析分析结果；结构失败或截断时最多重试一次。"""
        first_error = ""
        retry_prompt = user_prompt
        for attempt in range(2):
            response = self._call_ai(retry_prompt, system_prompt)
            used_model = getattr(self.client, "last_model", None) or ""
            if self._last_ai_call_was_truncated():
                reason = getattr(self.client, "last_finish_reason", None) or "length"
                result = AIAnalysisResult(
                    raw_response=response,
                    error=f"AI 输出被截断（finish_reason={reason}）",
                    model=used_model,
                )
            else:
                result = self._parse_response(response)
                result.model = used_model

            if result.success:
                return result
            self._dump_rejected_output(result, attempt + 1)
            if attempt == 0:
                first_error = result.error
                print(f"[AI] 输出校验失败，准备重试一次: {first_error}")
                retry_prompt = (
                    f"{user_prompt}\n\n"
                    f"上一版输出未通过校验：{first_error}\n"
                    "请重新生成完整简报，确保所有必选板块均有内容；"
                    "可选板块仅在模板规定的条件成立时输出，并严格保留规定标题。"
                )
                continue
            result.error = f"首次校验失败：{first_error}；重试仍失败：{result.error}"
            return result

        raise RuntimeError("AI 分析重试流程异常结束")

    def _dump_rejected_output(self, result: AIAnalysisResult, attempt: int) -> None:
        """质检失败时保存模型原始输出到 output/meta/，供人工复盘。"""
        try:
            response = (result.raw_response or "").strip()
            if not response:
                return
            meta_dir = Path("output") / "meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            stamp = self.get_time_func().strftime("%Y-%m-%d_%H%M%S")
            path = meta_dir / f"ai_reject_{stamp}_a{attempt}.md"
            header = (
                "<!-- AI 输出质检失败存档\n"
                f"     model: {result.model or 'unknown'}\n"
                f"     error: {result.error}\n"
                f"     attempt: {attempt}\n"
                "-->\n\n"
            )
            path.write_text(header + response + "\n", encoding="utf-8")
            print(f"[AI] 原始输出已存档: {path}")
        except Exception as exc:
            print(f"[AI] 质检失败存档异常: {type(exc).__name__}: {exc}")

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
    def _validate_events(title: str, content: str) -> str:
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
        # 总领允许整句或局部加粗（提示词要求首句以加粗短句给出全局总括）。
        # ** 与 __ 是格式标记，不参与句式判定；否则 "**…。**" 会因
        # 末尾不是句号而被误判，导致每次生成都校验失败。
        lead_text = re.sub(r"\*\*|__", "", lead).strip()
        sentence_ends = re.findall(r"[。！？]", lead_text)
        if len(sentence_ends) != 1 or not re.search(r"[。！？][”’」』】]?$", lead_text):
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

    @staticmethod
    def _validate_no_internal_index(title: str, content: str) -> str:
        """拦下内部事件编号泄漏。"""
        # 仅拦截同一行内的“事件 1”引用。`\s*` 会跨越换行，把
        # “### 支撑事件\n1. 主线”误判为内部事件编号。
        match = re.search(r"事件[ \t]*\d+", content)
        if match:
            return f"{title}不得引用内部事件编号（{match.group(0)}），请直接引用新闻标题"
        return ""

    @staticmethod
    def _validate_lead_points(title: str, content: str) -> str:
        """宽松校验「加粗总括句 + 分条拆解」。"""
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return f"{title}内容为空"
        lead = lines[0]
        if _LEAD_AS_POINT.match(lead):
            return f"{title}首行必须是独立的加粗总括句，不能直接从分条开始"
        if not re.search(r"(?:\*\*|__).+?(?:\*\*|__)", lead):
            return f"{title}首行总括句必须加粗"
        points = sum(1 for line in lines[1:] if _POINT_MARKER.match(line))
        if points < 1:
            return f"{title}必须在总括句后分条拆解至少 1 条主线"
        return ""

    def _parse_response(self, response: str) -> AIAnalysisResult:
        """解析固定板块；容忍标题层级、空格、冒号和顺序差异。"""
        if not response or not response.strip():
            return AIAnalysisResult(raw_response=response, error="AI 返回空响应")

        markdown = self._strip_markdown_fences(response)
        result = AIAnalysisResult(raw_response=response)
        specs = self.section_specs
        section_specs = {spec.title: spec for spec in specs}
        titles_pattern = "|".join(map(re.escape, section_specs))
        pattern = re.compile(
            rf"(?m)^\s*#{{1,3}}\s*({titles_pattern})\s*[:：]?\s*$"
        )
        matches = list(pattern.finditer(markdown))
        if not matches:
            result.error = "未识别规定的 Markdown 板块标题"
            return result

        matched_titles = [match.group(1) for match in matches]
        duplicate_titles = sorted({t for t in matched_titles if matched_titles.count(t) > 1})
        missing_titles = [
            spec.title
            for spec in specs
            if spec.required and spec.title not in matched_titles
        ]
        if duplicate_titles or missing_titles:
            problems = []
            if duplicate_titles:
                problems.append(f"重复标题：{'、'.join(duplicate_titles)}")
            if missing_titles:
                problems.append(f"缺少标题：{'、'.join(missing_titles)}")
            result.error = "Markdown 板块校验失败（" + "；".join(problems) + "）"
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
            # 内部编号泄漏兜底：适用于所有板块格式，不仅 events。
            leak = self._validate_no_internal_index(spec.title, content)
            if leak:
                result.error = leak
                return result
            if spec.format_type == "events":
                validation_error = self._validate_events(spec.title, content)
                if validation_error:
                    result.error = validation_error
                    return result
            elif spec.format_type == "lead_points":
                validation_error = self._validate_lead_points(spec.title, content)
                if validation_error:
                    result.error = validation_error
                    return result
            elif spec.format_type == "bullets" and any(
                line.strip() and not re.match(r"^-\s+\S", line.strip())
                for line in content.splitlines()
            ):
                result.error = f"{spec.title}必须只包含无序列表项"
                return result

        result.sections = [
            AIAnalysisSection(
                title=spec.title,
                content=contents[spec.title],
                format_type=spec.format_type,
            )
            for spec in specs
            if spec.title in contents
        ]
        result.success = True
        return result
