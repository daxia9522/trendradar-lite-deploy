# coding=utf-8
"""Prepare immutable report emails, then submit explicit envelope attempts."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from .models import EmailDeliveryResult, PreparedEmail
from .senders import prepare_email, send_prepared_email


class NotificationDispatcher:
    """Shared email entry point; no mutable delivery state or hidden retry loop."""

    def __init__(self, config: Dict[str, Any], get_time_func: Callable):
        self.config = config
        self.get_time_func = get_time_func

    def _configured(self) -> bool:
        return all(self.config.get(key) for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"))

    def prepare_report(
        self, report_type: str, html_file_path: Optional[str], *,
        period_name: Optional[str] = None, subject_override: Optional[str] = None,
        sender_name_override: Optional[str] = None,
    ) -> Optional[PreparedEmail]:
        """Freeze actual MIME bytes without connecting to SMTP or retaining secrets."""
        if not self._configured():
            missing = [key for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO") if not self.config.get(key)]
            print(f"[邮件] 缺少邮件配置: {', '.join(missing)}")
            return None
        if not html_file_path:
            print("[邮件] 缺少 HTML 文件路径，跳过")
            return None
        now = self.get_time_func()
        label = (period_name or report_type or "").strip() or "热点分析"
        try:
            return prepare_email(
                self.config["EMAIL_FROM"], self.config["EMAIL_TO"], report_type, html_file_path,
                get_time_func=lambda: now,
                subject_override=subject_override or f"{label} · {now.strftime('%m月%d日 %H:%M')}",
                sender_name_override=sender_name_override or label,
            )
        except Exception as exc:
            print(f"[邮件] 报告准备失败（{type(exc).__name__}）")
            return None

    def send_prepared(
        self, prepared: PreparedEmail, *, recipients: Optional[Sequence[str]] = None,
    ) -> EmailDeliveryResult:
        """Submit one attempt using current transport credentials and frozen payload.

        The configured sender/recipient list is intentionally not reapplied: a
        pending report has its own authorized original envelope. The caller must
        durably claim the attempt and then persist its returned receipt.
        """
        return send_prepared_email(
            prepared, recipients=recipients, password=self.config.get("EMAIL_PASSWORD", ""),
            custom_smtp_server=self.config.get("EMAIL_SMTP_SERVER") or None,
            custom_smtp_port=self.config.get("EMAIL_SMTP_PORT") or None,
        )

    def send_report(
        self, report_type: str, html_file_path: Optional[str] = None, *,
        period_name: Optional[str] = None, subject_override: Optional[str] = None,
        sender_name_override: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Prepare+send convenience for non-daily consumers; no implicit retry."""
        prepared = self.prepare_report(
            report_type, html_file_path, period_name=period_name,
            subject_override=subject_override, sender_name_override=sender_name_override,
        )
        if prepared is None:
            return EmailDeliveryResult(configured=self._configured())
        return self.send_prepared(prepared)
