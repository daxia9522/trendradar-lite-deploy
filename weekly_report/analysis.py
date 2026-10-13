"""Weekly analysis and keywords, without runtime, persistence or mail side effects.

Public stages: generate_report() then generate_keywords(). Prompt construction,
response parsing and each stage's retry policy stay local to this module.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trendradar.ai.client import AIClient, build_keyword_client
from .collection import is_column_title

# Keyword policy is not part of news collection/scoring.
KEYWORD_TOPN = 5
KEYWORD_TITLE_LIMIT = 100
HEADLINE_MIN_LEN = 2
HEADLINE_MAX_LEN = 4

STOPWORDS = {
    "今日", "本周", "最新", "热点", "表示", "消息", "中国", "美国", "公司", "市场", "已经", "进行", "相关", "发布", "报道", "工作", "记者",
    "快讯", "图示", "数据", "显示", "回应", "通报", "指出", "视频", "全文", "直播", "热搜", "话题", "头条", "网友", "全球", "国内", "国际",
    "同比", "环比", "财经", "财联社", "金十", "金十数据", "卫报", "BBC", "雅虎", "新华社", "中新网", "局势", "政策", "基建",
}

NOISE_WORDS = {
    "亿元", "美元", "万亿", "万亿元", "万元", "百亿", "千亿", "图示", "快讯", "消息", "市场消息", "金十图示", "最新动态", "边打边",
}

# 邮件头禁止/空泛词（方案 B）
HEADLINE_BANNED = {
    "亿元", "美元", "市场消息", "金十图示", "图示", "快讯", "热点", "新闻", "边打边", "最新动态",
    "中东局势", "AI基建", "特朗普政策", "全球市场", "避险情绪", "中国消费", "智能终端",
    "局势", "政策", "市场", "基建", "消息", "动态", "分析", "观察", "综述",
    # 真实周报规则兜底中出现过的空泛词与滑窗碎片
    "规模", "科技", "早盘", "盘收", "如何", "为何", "怎么", "哪些", "上涨", "发行", "最高", "年期",
    "利率", "倍数", "登陆", "风白海", "宇树科", "发行利率", "边际倍数",
    "早盘收", "风白", "倍数预",
}

# 债券发行行情是高频结构化数据，不适合从字符滑窗中提取周报主题。
# 整条标题跳过，避免完整词被过滤后又留下“行利/际倍”一类内部碎片。
RULE_DATA_TEMPLATE_TITLE_RE = re.compile(
    r"(发行利率|边际利率|投标倍数|边际倍数|倍数预期)"
)

# 允许略长的缩写/专名
HEADLINE_LEN_EXCEPTIONS = {
    "CPI", "GDP", "GPU", "Nvidia", "OpenAI", "ChatGPT", "WTI", "OPEC", "VIX",
}



def _is_hotlist_item(item: Dict[str, Any]) -> bool:
    """邮件头/实体热词只吃热榜（news/mixed），不含纯 RSS。"""
    return (item.get("source_type") or "news") in ("news", "mixed")


# ---- Prompt templates and evidence formatting ----

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEEKLY_AI_PROMPT_FILE = PROJECT_ROOT / "config" / "weekly_ai_prompt.txt"
WEEKLY_KEYWORD_PROMPT_FILE = PROJECT_ROOT / "config" / "weekly_keyword_prompt.txt"


def _load_prompt_template(prompt_path: Path) -> Tuple[str, str]:
    """加载提示词；仅识别单独成行的 [system] / [user]，缺文件或 user 空则硬失败。"""
    if not prompt_path.exists():
        raise FileNotFoundError(f"周报提示词不存在: {prompt_path}")
    lines = prompt_path.read_text(encoding="utf-8").splitlines()
    sections: Dict[str, List[str]] = {"system": [], "user": []}
    current: Optional[str] = None
    for raw in lines:
        token = raw.strip()
        if token == "[system]":
            current = "system"
            continue
        if token == "[user]":
            current = "user"
            continue
        if current:
            sections[current].append(raw)
    system_prompt = "\n".join(sections["system"]).strip()
    user_prompt = "\n".join(sections["user"]).strip()
    if not user_prompt:
        raise ValueError(f"周报提示词 user 段为空: {prompt_path}")
    return system_prompt, user_prompt


def _fill_prompt_template(template: str, mapping: Dict[str, str]) -> str:
    """按占位符替换；未知花括号保持原样。"""
    out = template
    for key, value in mapping.items():
        out = out.replace("{" + key + "}", value)
    return out


def _format_cluster_line(idx: int, item: Dict[str, Any], prefix: str = "") -> str:
    dates = item.get("dates") or []
    if len(dates) >= 2:
        date_part = f"{dates[0][5:]}~{dates[-1][5:]}" if len(dates[0]) >= 10 else f"{dates[0]}~{dates[-1]}"
    elif dates:
        date_part = dates[0][5:] if len(dates[0]) >= 10 else dates[0]
    else:
        date_part = "-"

    platforms = item.get("platforms") or []
    if len(platforms) > 3:
        plat_part = "/".join(platforms[:3]) + f"等{len(platforms)}台"
    else:
        plat_part = "/".join(platforms) if platforms else "-"

    best = item.get("best_rank")
    hi = item.get("rank_hi")
    if best is not None and hi is not None and best != hi:
        rank_part = f" | 排名:{best}-{hi}"
    elif best is not None:
        rank_part = f" | 排名:{best}"
    else:
        rank_part = ""

    span = item.get("date_span") or 1
    span_part = f" | 跨天:{span}" if span > 1 else ""
    return (
        f"{prefix}{idx}. [{date_part}] [{plat_part}] {item.get('title', '')} "
        f"| 分:{float(item.get('score') or 0):.1f} | 次:{item.get('count') or 1}{rank_part}{span_part}"
    )


def build_evidence_index(news_items: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    evidence: Dict[str, Dict[str, Any]] = {}
    n_i = r_i = 0
    for item in news_items:
        if item.get("source_type") == "rss":
            r_i += 1
            evidence[f"R{r_i}"] = item
        else:
            n_i += 1
            evidence[f"N{n_i}"] = item
    return evidence


def build_prompt(
    start_date: str,
    end_date: str,
    news_items: List[Dict[str, Any]],
    platform_counter: Dict[str, int],
    pipeline_stats: Optional[Dict[str, Any]] = None,
    prompt_path: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """填充 weekly_ai_prompt.txt。"""
    top_platforms = list(platform_counter.items())[:12]
    pipeline_stats = pipeline_stats or {}

    news_lines: List[str] = []
    rss_lines: List[str] = []
    n_i = r_i = 0
    for item in news_items:
        if item.get("source_type") == "rss":
            r_i += 1
            rss_lines.append(_format_cluster_line(r_i, item, prefix="R"))
        else:
            n_i += 1
            news_lines.append(_format_cluster_line(n_i, item, prefix="N"))

    stats_block = {
        "raw_news": pipeline_stats.get("raw_news"),
        "raw_rss": pipeline_stats.get("raw_rss"),
        "exact_total": pipeline_stats.get("exact_total"),
        "cluster_total": pipeline_stats.get("cluster_total"),
        "selected": pipeline_stats.get("selected", len(news_items)),
        "selected_news": pipeline_stats.get("selected_news"),
        "selected_rss": pipeline_stats.get("selected_rss"),
        "rss_day_coverage": pipeline_stats.get("rss_day_coverage"),
    }

    system_content, user_template = _load_prompt_template(
        prompt_path or WEEKLY_AI_PROMPT_FILE
    )
    user_content = _fill_prompt_template(
        user_template,
        {
            "start_date": start_date,
            "end_date": end_date,
            "stats_json": json.dumps(stats_block, ensure_ascii=False),
            "news_count": str(len(news_items)),
            "platforms_json": json.dumps(top_platforms, ensure_ascii=False),
            "news_content": "\n".join(news_lines) if news_lines else "（无）",
            "rss_content": "\n".join(rss_lines) if rss_lines else "（无）",
        },
    )
    messages: List[Dict[str, str]] = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_content})
    return messages


def build_keyword_prompt(
    start_date: str,
    end_date: str,
    report_text: str,
    prompt_path: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """填充 weekly_keyword_prompt.txt。"""
    system_content, user_template = _load_prompt_template(
        prompt_path or WEEKLY_KEYWORD_PROMPT_FILE
    )
    user_content = _fill_prompt_template(
        user_template,
        {
            "start_date": start_date,
            "end_date": end_date,
            "report_text": report_text,
        },
    )
    messages: List[Dict[str, str]] = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_content})
    return messages


# ---- Report analysis: selected news -> parsed body/themes/model ----

@dataclass(frozen=True)
class ReportAnalysis:
    report_text: str
    themes: List[Dict[str, Any]]
    model: str


def generate_report(
    client: AIClient,
    start_s: str,
    end_s: str,
    all_news: List[Dict[str, Any]],
    platform_counter: Dict[str, int],
    pipeline_stats: Optional[Dict[str, Any]] = None,
) -> Optional[ReportAnalysis]:
    """Use an already validated client; None means both report attempts failed."""
    # 主干①：周报正文（空结果/异常时再试 1 次；模型链 fallback 由 AIClient 处理）
    messages = build_prompt(
        start_s,
        end_s,
        all_news,
        platform_counter,
        pipeline_stats,
    )
    raw_report = ""
    last_report_err = ""
    used_model = ""
    for attempt in range(2):
        try:
            raw_report = (client.chat(messages) or "").strip()
            if raw_report:
                used_model = (getattr(client, "last_model", None) or "").strip()
                break
            last_report_err = "empty_response"
            print(f"[周报正文] 空响应 attempt={attempt + 1}")
        except Exception as exc:
            # AIClient re-raises the provider error; never format its body here.
            last_report_err = type(exc).__name__
            print(f"[周报正文] 调用失败 attempt={attempt + 1}: {last_report_err}")
    if not raw_report:
        print(f"周报正文生成失败: {last_report_err or 'unknown'}")
        return None

    evidence_index = build_evidence_index(all_news)
    report_text, themes = parse_structured_report(raw_report, evidence_index)
    return ReportAnalysis(report_text, themes, used_model)


# ---- Keyword extraction and validation (existing behavior retained) ----

def _headline_len_ok(label: str) -> bool:
    if label in HEADLINE_LEN_EXCEPTIONS:
        return True
    # 纯英文缩写放宽到 5
    if re.fullmatch(r"[A-Za-z0-9]{2,5}", label):
        return True
    return HEADLINE_MIN_LEN <= len(label) <= HEADLINE_MAX_LEN


def _is_banned_headline(label: str) -> bool:
    if label in HEADLINE_BANNED:
        return True
    # 保留“宇树科技/量子科技”类四字实体，过滤“科技发/树科技”等滑窗碎片。
    if "科技" in label and not (len(label) == 4 and label.endswith("科技")):
        return True
    for bad in ("局势", "政策", "市场", "基建", "热点", "新闻", "消息", "动态"):
        if label.endswith(bad) and len(label) <= 6:
            return True
    return False


def _normalize_headline_token(raw: str) -> str:
    p = re.sub(r"^[0-9一二三四五六七八九十]+[.、]\s*", "", (raw or "").strip())
    p = p.strip(' \n\t-—,，;；.。[]【】"\'“”')
    p = re.sub(r"\s+", "", p)
    return p


# 灾种/事件后缀：与标题中地名拼成「日本地震」类合成词（中间可夹其它字）
_EVENT_COMPOUND_SUFFIXES = (
    "地震", "台风", "暴雨", "山火", "海啸", "洪灾", "山洪", "泥石流", "崩塌",
)
_PLACE_HINT_RE = re.compile(
    r"(日本|中国|美国|伊朗|以色列|台湾|香港|澳门|新疆|西藏|青海|四川|云南|甘肃|"
    r"陕西|山西|河北|河南|山东|江苏|浙江|福建|广东|广西|湖南|湖北|安徽|江西|"
    r"辽宁|吉林|黑龙江|贵州|海南|重庆|北京|上海|天津|宁夏|内蒙古|"
    r"熊本|东京|大阪|北海道|台湾新北|新北|花莲|宜兰)"
)


def _iter_event_compounds(title: str) -> List[str]:
    """从标题合成事件切口，避免规则回退拆成「日本」「地震」。"""
    title = title or ""
    out: List[str] = []
    for sfx in _EVENT_COMPOUND_SUFFIXES:
        if sfx not in title:
            continue
        for m in _PLACE_HINT_RE.finditer(title):
            place = m.group(1)
            # 地名应出现在灾种之前（或同句），且合成后 3~4 字优先
            if m.start() > title.find(sfx):
                continue
            compound = f"{place}{sfx}"
            # 「台湾新北地震」过长则收成「台湾地震」
            if len(compound) > HEADLINE_MAX_LEN:
                if place.startswith("台湾") and len(f"台湾{sfx}") <= HEADLINE_MAX_LEN:
                    compound = f"台湾{sfx}"
                elif len(place) >= 2 and len(f"{place[:2]}{sfx}") <= HEADLINE_MAX_LEN:
                    compound = f"{place[:2]}{sfx}"
                else:
                    continue
            if HEADLINE_MIN_LEN <= len(compound) <= HEADLINE_MAX_LEN:
                out.append(compound)
        # 无地名命中但标题含「X级地震」等，保留灾种本身由 n-gram 处理
    return out


def _iter_short_title_tokens(title: str) -> List[str]:
    """标题短实体：英文专名 + 中文 2/3/4 元 + 事件合成词。"""
    title = title or ""
    out: List[str] = []
    for eng in re.findall(r"[A-Za-z][A-Za-z0-9]{1,7}", title):
        out.append(eng)
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", title):
        if 2 <= len(seg) <= 4:
            out.append(seg)
        for n in (2, 3, 4):
            if len(seg) < n:
                continue
            for i in range(0, len(seg) - n + 1):
                out.append(seg[i : i + n])
    out.extend(_iter_event_compounds(title))
    return out


def _merge_split_event_headlines(selected: List[str], titles: Sequence[str]) -> List[str]:
    """若已选「日本」「地震」且标题可支撑「日本地震」，合并为合成词。"""
    if not selected:
        return selected
    title_blob = "\n".join(titles or [])
    merged = list(selected)
    for sfx in _EVENT_COMPOUND_SUFFIXES:
        if sfx not in merged:
            continue
        places = [x for x in merged if x != sfx and x in title_blob and sfx in title_blob]
        # 仅当地名与灾种同现于至少一条标题
        for place in places:
            compound = f"{place}{sfx}"
            if len(compound) > HEADLINE_MAX_LEN:
                continue
            if not any(place in t and sfx in t for t in titles):
                continue
            # 用合成词替换 place+sfx（保留相对靠前位置）
            idx = min(merged.index(place), merged.index(sfx))
            merged = [x for x in merged if x not in (place, sfx)]
            if compound not in merged:
                merged.insert(idx, compound)
            break
    return merged


_RULE_FUNCTION_SUFFIXES = {
    "措施", "路径", "影响", "预期", "问题", "情况", "方面", "工作", "波动",
    "新高", "阶段", "市场", "股价", "利率", "倍数", "规模", "发行", "上涨", "最高", "年期",
}

_RULE_NOISE = {
    "相关", "进行",
    "表示", "指出", "回应", "报道", "最新", "今日", "本周", "创新", "大涨", "升温",
    "推动", "解读", "官员", "同向", "再创", "引发", "关注",
    "军方", "发布", "芯片", "同向", "押注", "谈及", "谈降", "科技", "早盘", "盘收", "登陆",
}



def _is_redundant_headline(token: str, selected: Sequence[str]) -> bool:
    for prev in selected:
        if token == prev:
            return True
        # 互相包含：保留已选更长词，跳过更短碎片
        if token in prev or prev in token:
            return True
        if len(token) >= 2 and len(prev) >= 2:
            inter = len(set(token) & set(prev))
            if inter / max(len(set(token) | set(prev)), 1) >= 0.7:
                return True
    return False


def build_rule_entity_headlines(news_items: List[Dict[str, Any]], topn: int = 5) -> List[str]:
    """AI 失败时的规则兜底：仅热榜标题，多标题共现短实体。"""
    scores: Counter = Counter()
    df: Counter = Counter()
    hotlist = [x for x in news_items if _is_hotlist_item(x)]
    for item in hotlist[:200]:
        title = item.get("title") or ""
        if item.get("is_column") or is_column_title(title) or RULE_DATA_TEMPLATE_TITLE_RE.search(title):
            continue
        weight = max(float(item.get("score") or 1.0), 1.0)
        seen_in_title = set()
        for token in _iter_short_title_tokens(title):
            token = _normalize_headline_token(token)
            if not token or _is_banned_headline(token):
                continue
            if token in STOPWORDS or token in NOISE_WORDS or token in _RULE_NOISE:
                continue
            # 以功能后缀结尾的词（如「关税措施」）降级剔除；
            # 该集合与普通噪声分开，避免误伤“宇树科技/存储芯片”等实体。
            if any(token.endswith(sfx) for sfx in _RULE_FUNCTION_SUFFIXES):
                continue
            if re.search(r"亿|万|美元|元|%", token):
                continue
            if not _headline_len_ok(token):
                continue
            if token.isascii():
                bonus = 1.5
            else:
                # 更长 n-gram 略加分，便于「英伟达」「以色列」压过「英伟」
                bonus = 0.75 + 0.25 * len(token)
            scores[token] += weight * bonus
            if token not in seen_in_title:
                df[token] += 1
                seen_in_title.add(token)

    ranked = sorted(
        (
            (token, sc * (0.15 + df[token] ** 1.6) * (1.05 if len(token) >= 3 or token.isascii() else 1.0))
            for token, sc in scores.items()
        ),
        key=lambda x: (-x[1], -len(x[0]), x[0]),
    )

    selected: List[str] = []
    for token, _ in ranked:
        # 宁可少于 5 个，也不使用只在单条标题中出现的滑窗碎片。
        if df[token] < 2:
            continue
        if _is_redundant_headline(token, selected):
            continue
        selected.append(token)
        if len(selected) >= topn:
            return selected
    return selected


def _parse_keyword_list(raw: str) -> List[str]:
    text = (raw or "").strip()
    if not text:
        return []
    # 去掉可能的代码围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.M).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    # 尝试截取首个 [...]
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass
    return [p.strip() for p in re.split(r"[/／|｜,，;；\n]", text) if p.strip()]


THEMES_BLOCK_RE = re.compile(
    r"<THEMES_JSON>\s*([\s\S]*?)\s*</THEMES_JSON>\s*",
    re.I,
)
REPORT_BLOCK_RE = re.compile(
    r"<REPORT_MARKDOWN>\s*([\s\S]*?)\s*</REPORT_MARKDOWN>",
    re.I,
)


def parse_structured_report(
    raw: str,
    evidence_index: Dict[str, Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """解析主题证据块；主题无效时仍保留正文，供独立关键词 Prompt 回退。"""
    text = (raw or "").strip()
    report_match = REPORT_BLOCK_RE.search(text)
    if report_match:
        report_text = report_match.group(1).strip()
    else:
        report_text = THEMES_BLOCK_RE.sub("", text).strip()
        report_text = re.sub(r"^\s*<REPORT_MARKDOWN>\s*", "", report_text, flags=re.I).strip()

    themes_match = THEMES_BLOCK_RE.search(text)
    if not themes_match:
        return report_text, []
    try:
        payload = json.loads(themes_match.group(1))
    except Exception:
        return report_text, []
    if not isinstance(payload, list):
        return report_text, []

    valid_themes: List[Dict[str, Any]] = []
    for raw_theme in payload[:8]:
        if not isinstance(raw_theme, dict):
            continue
        title = str(raw_theme.get("title") or "").strip()
        keyword = _normalize_headline_token(str(raw_theme.get("keyword") or ""))
        raw_ids = raw_theme.get("evidence_ids") or []
        evidence_ids = []
        for evidence_id in raw_ids if isinstance(raw_ids, list) else []:
            normalized_id = str(evidence_id).strip().upper()
            if normalized_id in evidence_index and normalized_id not in evidence_ids:
                evidence_ids.append(normalized_id)
        if not title or not keyword or _is_banned_headline(keyword) or not _headline_len_ok(keyword):
            continue
        if keyword not in report_text or len(evidence_ids) < 2:
            continue
        valid_themes.append(
            {"title": title, "keyword": keyword, "evidence_ids": evidence_ids[:5]}
        )
    return report_text, valid_themes


def keywords_from_themes(themes: Sequence[Dict[str, Any]], topn: int = KEYWORD_TOPN) -> List[str]:
    return _filter_headlines([str(theme.get("keyword") or "") for theme in themes])[:topn]


def _filter_headlines(
    candidates: Sequence[str],
    report_text: str = "",
) -> List[str]:
    cleaned: List[str] = []
    for p in candidates:
        if not isinstance(p, str):
            continue
        p = _normalize_headline_token(p)
        if not p or _is_banned_headline(p):
            continue
        if report_text and p not in report_text:
            continue
        if not _headline_len_ok(p):
            if re.fullmatch(r"[\u4e00-\u9fff]{5,8}", p):
                p = p[:4]
                if _is_banned_headline(p) or not _headline_len_ok(p):
                    continue
            else:
                continue
        if p not in cleaned:
            cleaned.append(p)
        if len(cleaned) >= KEYWORD_TOPN:
            break
    return cleaned


def extract_headline_keywords(
    client: AIClient,
    start_date: str,
    end_date: str,
    news_items: List[Dict[str, Any]],
    report_text: str,
    title_limit: int = KEYWORD_TITLE_LIMIT,
    topn: int = KEYWORD_TOPN,
) -> Tuple[List[str], str]:
    """从周报正文提炼邮件头关键词，并用热榜标题核验支撑。"""
    titles: List[str] = []
    for item in news_items:
        if not _is_hotlist_item(item):
            continue
        if item.get("is_column") or is_column_title(item.get("title") or ""):
            continue
        t = (item.get("title") or "").strip()
        if t and t not in titles:
            titles.append(t)
        if len(titles) >= title_limit:
            break

    rule_fallback = _filter_headlines(
        build_rule_entity_headlines(news_items, topn=max(topn * 4, 20)),
        report_text,
    )
    rule_fallback = _filter_headlines(
        _merge_split_event_headlines(rule_fallback, titles),
        report_text,
    )
    if not titles:
        return rule_fallback[:topn], "rule_only_empty_titles"

    keyword_client = build_keyword_client(client)
    messages = build_keyword_prompt(start_date, end_date, report_text)
    last_error = ""
    for attempt in range(2):
        try:
            raw = keyword_client.chat(messages)
            parsed = _parse_keyword_list(raw)
            cleaned = _filter_headlines(parsed, report_text)
            cleaned = _filter_headlines(
                _merge_split_event_headlines(cleaned, titles),
                report_text,
            )
            for p in rule_fallback:
                if p not in cleaned:
                    cleaned.append(p)
                if len(cleaned) >= topn:
                    break
            if len(cleaned) >= 3:
                cleaned = _filter_headlines(
                    _merge_split_event_headlines(cleaned[:topn], titles),
                    report_text,
                )
                src = "ai_lite" if attempt == 0 else "ai_lite_retry"
                return cleaned[:topn], src
            # Invalid provider output can also echo request details; log counts only.
            last_error = f"valid_labels={len(cleaned)}"
            print(f"[关键词] lite 无效输出 attempt={attempt + 1}: {last_error}")
        except Exception as e:
            # Retain only the category for both the attempt and final fallback log.
            last_error = type(e).__name__
            print(f"[关键词] lite 抽取失败 attempt={attempt + 1}: {last_error}")

    print(f"[关键词] 回退规则实体 Top{topn}（{last_error}）")
    return rule_fallback[:topn], "rule_only"


def generate_keywords(
    client: AIClient,
    start_s: str,
    end_s: str,
    all_news: List[Dict[str, Any]],
    report_text: str,
    themes: Sequence[Dict[str, Any]],
) -> Tuple[List[str], str]:
    """Select existing theme labels first, then fill from the independent branch."""
    headline_keywords = keywords_from_themes(themes)
    if len(headline_keywords) >= KEYWORD_TOPN:
        keyword_source = "structured_themes"
    else:
        # 保留已验证的主题关键词，独立 Prompt 仅补足缺额。
        fallback_keywords, fallback_source = extract_headline_keywords(
            client,
            start_s,
            end_s,
            all_news,
            report_text,
        )
        for keyword in fallback_keywords:
            if keyword not in headline_keywords:
                headline_keywords.append(keyword)
            if len(headline_keywords) >= KEYWORD_TOPN:
                break
        keyword_source = (
            f"structured_themes+{fallback_source}"
            if themes
            else fallback_source
        )

    return headline_keywords, keyword_source
