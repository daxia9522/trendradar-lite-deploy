"""Headline extraction, structured AI response parsing, and fallbacks."""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Sequence, Tuple

from trendradar.ai.client import AIClient, build_keyword_client
from .prompting import build_keyword_prompt
from .collection import (
    STOPWORDS, NOISE_WORDS, RULE_DATA_TEMPLATE_TITLE_RE, KEYWORD_TOPN,
    KEYWORD_TITLE_LIMIT,
    HEADLINE_BANNED, HEADLINE_LEN_EXCEPTIONS, HEADLINE_MIN_LEN, HEADLINE_MAX_LEN,
    is_column_title,
    _is_hotlist_item, _title_similarity,
)


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

    keyword_client = build_keyword_client(
        {
            "MODEL": client.model,
            "API_KEY": client.api_key,
            "API_BASE": client.api_base,
            "TIMEOUT": client.timeout,
            "FALLBACK_MODELS": list(client.fallback_models),
        }
    )
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
            preview = re.sub(r"\s+", " ", (raw or "").strip())[:80]
            last_error = f"valid_labels={len(cleaned)} raw={preview!r}"
            print(f"[关键词] lite 无效输出 attempt={attempt + 1}: {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[关键词] lite 抽取失败 attempt={attempt + 1}: {e}")

    print(f"[关键词] 回退规则实体 Top{topn}（{last_error}）")
    return rule_fallback[:topn], "rule_only"
