# coding=utf-8
"""邮件通知模块。"""

from trendradar.notification.senders import send_to_email, SMTP_CONFIGS
from trendradar.notification.dispatcher import NotificationDispatcher

__all__ = [
    "send_to_email",
    "SMTP_CONFIGS",
    "NotificationDispatcher",
]
