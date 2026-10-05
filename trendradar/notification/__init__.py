# coding=utf-8
"""邮件通知模块。"""

from trendradar.notification.senders import send_to_email, send_prepared_email, SMTP_CONFIGS
from trendradar.notification.models import EmailDeliveryResult, PreparedEmail
from trendradar.notification.dispatcher import NotificationDispatcher

__all__ = [
    "send_to_email",
    "send_prepared_email",
    "PreparedEmail",
    "SMTP_CONFIGS",
    "EmailDeliveryResult",
    "NotificationDispatcher",
]
