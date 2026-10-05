#!/usr/bin/env python3
"""CLI orchestration for the AI weekly report email job.

Data, analysis, rendering and runtime behavior live in their owning modules.
This file retains the deployed CLI path.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trendradar.ai.client import AIClient
from trendradar.core.loader import load_ai_config
from trendradar.notification import NotificationDispatcher
from trendradar.report.weekly import render_weekly_html
from weekly_report.runtime import (
    report_now,
    load_runtime_env,
    resolve_date_range,
)
from weekly_report.collection import KEYWORD_TOPN, collect_news
from weekly_report.prompting import (
    build_evidence_index,
    build_prompt,
)
from weekly_report.keywords import (
    build_rule_entity_headlines, parse_structured_report, keywords_from_themes,
    extract_headline_keywords,
)

OUTPUT_DIR = PROJECT_ROOT / "output" / "weekly-ai-reports"
PARTIAL_EMAIL_EXIT_CODE = 6
HISTORY_READ_EXIT_CODE = 7


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AI 版 TrendRadar 周报并通过邮件发送")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--to", help="覆盖收件人，多个用逗号分隔")
    parser.add_argument("--subject", help="覆盖邮件主题")
    parser.add_argument("--model", default="", help="可选：覆盖 AI_MODEL（本地调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告文件，不发送邮件")
    args = parser.parse_args()

    env = load_runtime_env()

    start_date, end_date = resolve_date_range(args.start, args.end)
    try:
        all_news, platform_counter, pipeline_stats = collect_news(
            start_date,
            end_date,
        )
    except Exception as exc:
        # Fail before AI, report writes or email. Keep raw exception text and
        # tracebacks (which may contain credentials) out of scheduled job logs.
        print(f"周报历史读取失败，已中止: {type(exc).__name__}")
        return HISTORY_READ_EXIT_CODE
    if not all_news:
        print("未读取到可用于生成周报的数据")
        return 1
    ai_config = load_ai_config(str(PROJECT_ROOT / "config" / "config.yaml"))
    if (args.model or "").strip():
        ai_config["MODEL"] = args.model.strip()
    ai_model = str(ai_config.get("MODEL") or "").strip()
    if not ai_model:
        print("未配置 AI 模型，请设置 AI_MODEL 或 config.yaml 的 ai.model")
        return 2
    client = AIClient(ai_config)
    ok, error = client.validate_config()
    if not ok:
        print(error)
        return 2

    start_s = start_date.strftime("%Y-%m-%d")
    end_s = end_date.strftime("%Y-%m-%d")

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
        return 5

    evidence_index = build_evidence_index(all_news)
    report_text, themes = parse_structured_report(raw_report, evidence_index)
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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = report_now().strftime("%Y%m%d-%H%M%S")
    dr = f"{start_s} ~ {end_s}"
    base_name = f"weekly-ai-{start_s}-to-{end_s}-{stamp}"
    html_path = OUTPUT_DIR / f"{base_name}.html"
    keywords_path = OUTPUT_DIR / f"{base_name}.keywords.json"

    title = "AI 每周新闻分析"
    stats = {
        "去重/送入簇": len(all_news),
        "覆盖平台": len(platform_counter),
        "Top5关键词": " / ".join(headline_keywords) if headline_keywords else "-",
        "关键词来源": keyword_source,
        "raw_news": pipeline_stats.get("raw_news"),
        "raw_rss": pipeline_stats.get("raw_rss"),
        "exact": pipeline_stats.get("exact_total"),
        "cluster": pipeline_stats.get("cluster_total"),
    }
    # Header 展示正文分析实际模型（含 fallback），非仅配置主模型
    html_path.write_text(
        render_weekly_html(
            title, dr, used_model or ai_model, stats, report_text,
            generated_at=report_now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
        encoding="utf-8",
    )
    keywords_path.write_text(
        json.dumps(
            {
                "date_range": {"start": start_s, "end": end_s},
                "final": headline_keywords,
                "source": keyword_source,
                "themes": themes,
                "rule_fallback": build_rule_entity_headlines(all_news, topn=12),
                "pipeline": pipeline_stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"HTML: {html_path}")
    print(f"Keywords: {keywords_path}")
    print(json.dumps(stats, ensure_ascii=False))

    if args.dry_run:
        print("dry-run: 已跳过邮件发送")
        return 0

    # --to 仅覆盖本次发送；保留 EMAIL_* 环境配置来源，不修改共享映射。
    email_config = dict(env)
    if args.to:
        email_config["EMAIL_TO"] = args.to
    # 周报保留成对覆盖的严格要求；日报仍由 sender 回退到服务商默认值。
    # 凭据缺失优先交给共享入口报告，避免 SMTP 配对错误掩盖原有诊断。
    if all(email_config.get(key) for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO")):
        smtp_server = email_config.get("EMAIL_SMTP_SERVER")
        smtp_port = email_config.get("EMAIL_SMTP_PORT")
        if bool(smtp_server) != bool(smtp_port):
            missing = "EMAIL_SMTP_PORT" if smtp_server else "EMAIL_SMTP_SERVER"
            print(f"[邮件] 缺少邮件配置: {missing}")
            return 3
    dispatcher = NotificationDispatcher(email_config, datetime.now)
    final_subject = args.subject or f"AI周报（{dr}）"
    result = dispatcher.send_report(
        report_type=final_subject,
        html_file_path=str(html_path),
        subject_override=final_subject,
        sender_name_override="AI周报",
    )
    if not result.configured:
        return 3
    if result.unknown:
        # The deployed scheduler treats code 6 as terminal, not success. Reuse
        # that no-resend disposition: restarting the whole weekly task could
        # duplicate a DATA submission whose response was lost.
        print("[邮件] SMTP 提交结果未知，不自动重发；请核对服务商投递记录。")
        return PARTIAL_EMAIL_EXIT_CODE
    if result.partially_delivered:
        return PARTIAL_EMAIL_EXIT_CODE
    return 0 if result.sent else 4


if __name__ == "__main__":
    raise SystemExit(main())
