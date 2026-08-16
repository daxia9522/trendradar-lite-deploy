# coding=utf-8
"""通知调度器（精简版：仅邮件）。"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .senders import send_to_email


class NotificationDispatcher:
    """仅分发邮件。"""

    def __init__(
        self,
        config: Dict[str, Any],
        get_time_func: Callable,
    ):
        self.config = config
        self.get_time_func = get_time_func

    def _email_ready(self) -> bool:
        return bool(
            self.config.get("EMAIL_FROM")
            and self.config.get("EMAIL_PASSWORD")
            and self.config.get("EMAIL_TO")
        )

    def _send_email(
        self,
        report_type: str,
        html_file_path: Optional[str],
        *,
        period_name: Optional[str] = None,
        subject_override: Optional[str] = None,
        sender_name_override: Optional[str] = None,
    ) -> bool:
        if not html_file_path:
            print("[邮件] 缺少 HTML 文件路径，跳过")
            return False
        now = self.get_time_func()
        label = (period_name or report_type or "").strip() or "热点分析"
        sender_label = sender_name_override or label
        subject = subject_override or (
            f"{label} · {now.strftime('%m月%d日 %H:%M')}"
        )
        port = self.config.get("EMAIL_SMTP_PORT") or None
        return send_to_email(
            from_email=self.config["EMAIL_FROM"],
            password=self.config["EMAIL_PASSWORD"],
            to_email=self.config["EMAIL_TO"],
            report_type=report_type,
            html_file_path=html_file_path,
            custom_smtp_server=self.config.get("EMAIL_SMTP_SERVER") or None,
            custom_smtp_port=int(port) if port else None,
            get_time_func=self.get_time_func,
            subject_override=subject,
            sender_name_override=sender_label,
        )

    def dispatch_all(
        self,
        report_type: str,
        html_file_path: Optional[str] = None,
        *,
        period_name: Optional[str] = None,
    ) -> Dict[str, bool]:
        """发送已生成的邮件 HTML。"""
        results: Dict[str, bool] = {}
        if self._email_ready():
            results["email"] = self._send_email(
                report_type,
                html_file_path,
                period_name=period_name,
            )
        return results
