# coding=utf-8
"""发送一封报告邮件；SMTP 协议与重试仍由 send_to_email 负责。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .senders import send_to_email


@dataclass(frozen=True)
class EmailDeliveryResult:
    """一次发送的独立结果；部分接受不算成功，也不能整封自动重发。"""

    configured: bool
    sent: bool
    partially_delivered: bool

    def __bool__(self) -> bool:
        raise TypeError("请显式检查 configured、sent 或 partially_delivered")


class NotificationDispatcher:
    """报告邮件入口，保留历史类名供日报上下文使用。"""

    def __init__(
        self,
        config: Dict[str, Any],
        get_time_func: Callable,
    ):
        self.config = config
        self.get_time_func = get_time_func

    def send_report(
        self,
        report_type: str,
        html_file_path: Optional[str] = None,
        *,
        period_name: Optional[str] = None,
        subject_override: Optional[str] = None,
        sender_name_override: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """使用当前配置发送一封邮件，不维护收件人队列或上层重试。"""
        missing = [
            key for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO")
            if not self.config.get(key)
        ]
        smtp_server = self.config.get("EMAIL_SMTP_SERVER") or None
        smtp_port = self.config.get("EMAIL_SMTP_PORT") or None
        if missing:
            print(f"[邮件] 缺少邮件配置: {', '.join(missing)}")
            return EmailDeliveryResult(configured=False, sent=False, partially_delivered=False)

        if not html_file_path:
            print("[邮件] 缺少 HTML 文件路径，跳过")
            return EmailDeliveryResult(configured=True, sent=False, partially_delivered=False)
        now = self.get_time_func()
        label = (period_name or report_type or "").strip() or "热点分析"
        sender_label = sender_name_override or label
        subject = subject_override or (
            f"{label} · {now.strftime('%m月%d日 %H:%M')}"
        )
        partially_delivered = False

        def mark_partial_delivery() -> None:
            nonlocal partially_delivered
            partially_delivered = True

        sent = send_to_email(
            from_email=self.config["EMAIL_FROM"],
            password=self.config["EMAIL_PASSWORD"],
            to_email=self.config["EMAIL_TO"],
            report_type=report_type,
            html_file_path=html_file_path,
            custom_smtp_server=smtp_server,
            custom_smtp_port=int(smtp_port) if smtp_port else None,
            get_time_func=self.get_time_func,
            subject_override=subject,
            sender_name_override=sender_label,
            on_partial_delivery=mark_partial_delivery,
        )
        return EmailDeliveryResult(
            configured=True,
            sent=sent and not partially_delivered,
            partially_delivered=partially_delivered,
        )
