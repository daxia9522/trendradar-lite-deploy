"""Weekly report and label generation; no runtime, storage or mail side effects.

The two public stages are generate_report() and generate_keywords(). Labels
come only from the dedicated first fallback; validated quotes are provenance,
not proof of semantic entailment or word boundaries.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trendradar.ai.client import AIClient, build_keyword_client

# Prompt requests 2-6 characters; this guard retains the accepted 12-character ceiling.
KEYWORD_TOPN = 5
HEADLINE_MIN_LEN = 2
HEADLINE_MAX_LEN = 12
KEYWORD_EVIDENCE_MAX_LEN = 240


HEADLINE_BANNED = {
    "亿元", "美元", "市场消息", "金十图示", "图示", "快讯", "热点", "新闻", "边打边", "最新动态",
    "中东局势", "AI基建", "特朗普政策", "全球市场", "避险情绪", "中国消费", "智能终端",
    "局势", "政策", "市场", "基建", "消息", "动态", "分析", "观察", "综述",
    # 真实周报规则兜底中出现过的空泛词与滑窗碎片
    "规模", "科技", "早盘", "盘收", "如何", "为何", "怎么", "哪些", "上涨", "发行", "最高", "年期",
    "利率", "倍数", "登陆", "风白海", "宇树科", "发行利率", "边际倍数",
    "早盘收", "风白", "倍数预",
}


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEEKLY_AI_PROMPT_FILE = PROJECT_ROOT / "config" / "weekly_ai_prompt.txt"
WEEKLY_KEYWORD_PROMPT_FILE = PROJECT_ROOT / "config" / "weekly_keyword_prompt.txt"


# ---- Prompt templates and evidence formatting ----


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


def build_keyword_prompt(start_date: str, end_date: str, report_text: str, prompt_path: Optional[Path] = None) -> List[Dict[str, str]]:
    system_content, user_template = _load_prompt_template(prompt_path or WEEKLY_KEYWORD_PROMPT_FILE)
    user_content = _fill_prompt_template(user_template, {
        "start_date": start_date, "end_date": end_date, "report_text": report_text,
        "max_labels": str(KEYWORD_TOPN), "label_max_chars": str(HEADLINE_MAX_LEN),
        "evidence_max_chars": str(KEYWORD_EVIDENCE_MAX_LEN),
    })
    messages: List[Dict[str, str]] = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_content})
    return messages


# ---- Analysis response parsing and report generation ----


THEMES_BLOCK_RE = re.compile(
    r"<THEMES_JSON>\s*([\s\S]*?)\s*</THEMES_JSON>\s*",
    re.I,
)


REPORT_BLOCK_RE = re.compile(
    r"<REPORT_MARKDOWN>\s*([\s\S]*?)\s*</REPORT_MARKDOWN>",
    re.I,
)


def parse_structured_report(raw: str, evidence_index: Dict[str, Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Keep analysis themes/IDs. Main-model keyword fields never select final labels."""
    text = (raw or "").strip()
    report_match = REPORT_BLOCK_RE.search(text)
    if report_match:
        report_text = report_match.group(1).strip()
    else:
        report_text = THEMES_BLOCK_RE.sub("", text).strip()
        report_text = re.sub(r"^\s*<REPORT_MARKDOWN>\s*", "", report_text, flags=re.I).strip()
    match = THEMES_BLOCK_RE.search(text)
    if not match:
        return report_text, []
    try:
        payload = json.loads(match.group(1))
    except (ValueError, TypeError):
        return report_text, []
    if not isinstance(payload, list):
        return report_text, []
    themes: List[Dict[str, Any]] = []
    for theme in payload[:8]:
        if not isinstance(theme, dict) or not isinstance(theme.get("title"), str):
            continue
        title = theme["title"].strip()
        ids = theme.get("evidence_ids")
        if not title or not isinstance(ids, list):
            continue
        valid_ids = []
        for evidence_id in ids:
            if isinstance(evidence_id, str):
                evidence_id = evidence_id.strip().upper()
                if evidence_id in evidence_index and evidence_id not in valid_ids:
                    valid_ids.append(evidence_id)
        if len(valid_ids) >= 2:
            themes.append({"title": title, "evidence_ids": valid_ids[:5]})
    return report_text, themes


@dataclass(frozen=True)
class ReportAnalysis:
    report_text: str
    themes: List[Dict[str, Any]]
    model: str
    raw_themes: Optional[str] = None


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
    raw_themes = THEMES_BLOCK_RE.search(raw_report)
    return ReportAnalysis(report_text, themes, used_model, raw_themes.group(1) if raw_themes else None)


# ---- Independent labels and bounded structural/quote validation ----


def _normalize_headline_token(raw: str) -> str:
    """Remove only explicit outer formatting; preserve names and internal spaces."""
    text = raw.strip()
    # Labels are JSON fields, not list items: "7.5级地震" is not numbering.
    wrappers = {'"': '"', "'": "'", '“': '”', '‘': '’', '【': '】', '[': ']'}
    while len(text) >= 2 and wrappers.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def validate_keyword(candidate: Any, report_text: str) -> Tuple[Optional[Dict[str, str]], str]:
    """Validate shape and provenance, NOT semantic entailment or Chinese word boundaries."""
    if not isinstance(candidate, dict):
        return None, "invalid_item_type"
    if not isinstance(candidate.get("label"), str) or not isinstance(candidate.get("evidence"), str):
        return None, "invalid_field_type"
    label = _normalize_headline_token(candidate["label"])
    evidence = candidate["evidence"].strip()
    if not label:
        return None, "empty_label"
    if len(label) < HEADLINE_MIN_LEN:
        return None, "too_short"
    if len(label) > HEADLINE_MAX_LEN:
        return None, "too_long"
    if any(ord(char) < 32 for char in label):
        return None, "control_character"
    if label in HEADLINE_BANNED:
        return None, "banned_label"
    if not evidence:
        return None, "empty_evidence"
    if len(evidence) > KEYWORD_EVIDENCE_MAX_LEN:
        return None, "evidence_too_long"
    if evidence not in report_text:
        return None, "evidence_not_in_report"
    return {"label": label, "evidence": evidence}, "accepted"


def generate_keywords(client: AIClient, start_date: str, end_date: str, report_text: str) -> Dict[str, Any]:
    """Dedicated first-fallback model; at most two business attempts, no rule backfill."""
    result: Dict[str, Any] = {
        "version": 2, "source": "keyword_unavailable", "model": None,
        "final": [], "final_details": [], "attempts": [],
    }
    try:
        # Keep the complete native fallback:1 identity, including its own auth/base.
        # A missing slot must not silently promote the main model to label generation.
        if not any(candidate.id == "fallback:1" for candidate in client.candidates):
            result["reason"] = "missing_keyword_model"
            return result
        if not isinstance(report_text, str) or not report_text.strip():
            result["reason"] = "empty_report"
            return result
        keyword_client = build_keyword_client(client)
        if len(keyword_client.candidates) != 1 or keyword_client.candidates[0].id != "fallback:1":
            result["reason"] = "keyword_model_contract_mismatch"
            return result
        result["model"] = keyword_client.model
        messages = build_keyword_prompt(start_date, end_date, report_text)
    except Exception as exc:
        result["reason"] = "keyword_setup_error"
        result["error_type"] = type(exc).__name__
        return result

    for attempt in range(1, 3):
        audit: Dict[str, Any] = {"attempt": attempt, "model": result["model"], "raw": None, "candidates": []}
        result["attempts"].append(audit)
        retry_reason = ""
        try:
            raw = keyword_client.chat(messages)
            audit["model"] = getattr(keyword_client, "last_model", None) or keyword_client.model
            result["model"] = audit["model"]
            audit["raw"] = raw if isinstance(raw, str) else None
            if not isinstance(raw, str):
                raise ValueError("keyword response must be text")
            text = raw.strip()
            fence = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.I)
            if fence:
                text = fence.group(1)
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                payload = None
                retry_reason = "invalid_json"
            if not isinstance(payload, list):
                retry_reason = retry_reason or "not_json_array"
                audit["status"] = retry_reason
            else:
                seen = set()
                for index, candidate in enumerate(payload):
                    validated, reason = validate_keyword(candidate, report_text)
                    row: Dict[str, Any] = {
                        "index": index, "source": "keyword_ai", "raw": candidate,
                        "normalized": (
                            {"label": _normalize_headline_token(candidate["label"]),
                             "evidence": candidate["evidence"].strip()}
                            if isinstance(candidate, dict) and isinstance(candidate.get("label"), str)
                            and isinstance(candidate.get("evidence"), str) else None
                        ), "accepted": False, "reason": reason,
                    }
                    if validated:
                        label = validated["label"]
                        if label in seen:
                            row["reason"] = "duplicate"
                        elif len(result["final"]) >= KEYWORD_TOPN:
                            row["reason"] = "capacity_limit"
                        else:
                            seen.add(label)
                            result["final"].append(label)
                            result["final_details"].append({
                                **validated, "source": "keyword_ai", "model": audit["model"],
                                "attempt": attempt, "candidate_index": index,
                            })
                            row["accepted"] = True
                    audit["candidates"].append(row)
                if result["final"]:
                    audit["status"] = "accepted"
                    result["source"] = "keyword_ai" if attempt == 1 else "keyword_ai_retry"
                    return result
                retry_reason = "no_valid_labels"
                audit["status"] = retry_reason
        except Exception as exc:
            # Exception text can contain keys/request URLs; never persist or print it.
            audit["status"] = "request_error"
            audit["error_type"] = type(exc).__name__
            status = getattr(exc, "status_code", None)
            if isinstance(status, int):
                audit["http_status"] = status
            retry_reason = "request_error"
        if attempt == 1 and retry_reason != "request_error":
            reasons = sorted({x["reason"] for x in audit["candidates"]})
            messages = messages + [{
                "role": "user",
                "content": "上次输出未通过格式或引用校验：" + retry_reason +
                    ("；" + ", ".join(reasons) if reasons else "") +
                    "。请重新只输出label/evidence对象数组，evidence逐字引用上述正文。",
            }]
    result["source"] = "keyword_empty"
    result["reason"] = "attempts_exhausted"
    return result
