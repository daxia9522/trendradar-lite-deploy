"""Prompt template loading and evidence formatting."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
