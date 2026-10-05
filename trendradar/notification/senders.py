# coding=utf-8
"""Frozen MIME preparation and one-attempt SMTP submission.

There is intentionally no internal retry loop. The publication owner persists a
receipt before claiming another attempt for its temporary_failed subset. Losing
a DATA response is unknown, not proof of failure; QUIT errors cannot revoke an
acknowledged acceptance.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from datetime import datetime
from email import policy
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, format_datetime, make_msgid
from pathlib import Path
from typing import Callable, Optional, Sequence

from .models import EmailDeliveryResult, PreparedEmail

SMTP_CONFIGS = {
    "gmail.com": {"server": "smtp.gmail.com", "port": 587, "encryption": "TLS"},
    "qq.com": {"server": "smtp.qq.com", "port": 465, "encryption": "SSL"},
    "outlook.com": {"server": "smtp-mail.outlook.com", "port": 587, "encryption": "TLS"},
    "hotmail.com": {"server": "smtp-mail.outlook.com", "port": 587, "encryption": "TLS"},
    "live.com": {"server": "smtp-mail.outlook.com", "port": 587, "encryption": "TLS"},
    "163.com": {"server": "smtp.163.com", "port": 465, "encryption": "SSL"},
    "126.com": {"server": "smtp.126.com", "port": 465, "encryption": "SSL"},
    "sina.com": {"server": "smtp.sina.com", "port": 465, "encryption": "SSL"},
    "sohu.com": {"server": "smtp.sohu.com", "port": 465, "encryption": "SSL"},
    "189.cn": {"server": "smtp.189.cn", "port": 465, "encryption": "SSL"},
    "aliyun.com": {"server": "smtp.aliyun.com", "port": 465, "encryption": "SSL"},
    "yandex.com": {"server": "smtp.yandex.com", "port": 465, "encryption": "SSL"},
    "icloud.com": {"server": "smtp.mail.me.com", "port": 587, "encryption": "TLS"},
    "vip.163.com": {"server": "smtp.vip.163.com", "port": 465, "encryption": "SSL"},
}
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def prepare_email(
    from_email: str, to_email: str, report_type: str, html_file_path: str, *,
    get_time_func: Optional[Callable] = None,
    subject_override: Optional[str] = None,
    sender_name_override: Optional[str] = None,
) -> PreparedEmail:
    """Read HTML exactly once and serialize MIME exactly once; no network I/O."""
    sender = from_email.strip()
    recipients = tuple(dict.fromkeys(addr.strip() for addr in to_email.split(",") if addr.strip()))
    if not _EMAIL_RE.fullmatch(sender) or not recipients or any(
        not _EMAIL_RE.fullmatch(addr) for addr in recipients
    ):
        raise ValueError("Invalid email envelope")
    # smtplib's default envelope is ASCII. Reject unsupported addresses before
    # contacting a server rather than accidentally sending a partial transaction.
    if not all(addr.isascii() for addr in (sender, *recipients)):
        raise ValueError("SMTPUTF8 envelopes are not supported")
    html_content = Path(html_file_path).read_text(encoding="utf-8")
    now = get_time_func() if get_time_func else datetime.now().astimezone()
    subject = subject_override or f"TrendRadar 热点分析报告 - {report_type} - {now.strftime('%m月%d日 %H:%M')}"
    sender_name = sender_name_override or "TrendRadar"
    if any("\r" in value or "\n" in value for value in (subject, sender_name)):
        raise ValueError("Invalid email header")
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    date = format_datetime(now.astimezone() if now.tzinfo is None else now)
    # Explicit domain avoids hostname/DNS lookup and leaking the worker host.
    message_id = make_msgid(domain=sender.rsplit("@", 1)[1])
    msg["Date"] = date
    msg["Message-ID"] = message_id
    text_content = (
        f"{sender_name} 热点分析报告\n========================\n"
        f"报告类型：{report_type}\n生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "请使用支持HTML的邮件客户端查看完整报告内容。\n"
    )
    msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    return PreparedEmail(sender, recipients, subject, msg.as_bytes(policy=policy.SMTP), message_id, date)


def _code_outcome(code: int, *, submitted: bool = False) -> str:
    if 400 <= code < 500:
        return "temporary_failed"
    if 500 <= code < 600:
        return "permanent_failed"
    # Unexpected replies after DATA cannot establish acceptance or rejection.
    return "unknown" if submitted else "permanent_failed"


def _exception_outcome(exc: Exception, *, submitted: bool) -> str:
    if isinstance(exc, smtplib.SMTPResponseException):
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            # Stop even for temporary auth rejection to avoid account lockout.
            return "permanent_failed"
        return _code_outcome(exc.smtp_code, submitted=submitted)
    if submitted:
        return "unknown"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "permanent_failed"
    if isinstance(exc, (OSError, smtplib.SMTPServerDisconnected)):
        return "temporary_failed"
    return "permanent_failed"


def _deliver_once(
    *, smtp_server: str, smtp_port: int, use_tls: bool,
    tls_context: ssl.SSLContext, prepared: PreparedEmail, password: str,
    recipients: tuple[str, ...],
) -> EmailDeliveryResult:
    """Track MAIL/RCPT/DATA separately so a lost DATA reply keeps refusals.

    Before DATA no message can have been accepted, even if some RCPT commands
    succeeded. After starting DATA, only an explicit final reply is conclusive.
    A process interruption escapes to the ledger's inflight/unknown recovery.
    """
    server = None
    submitted = False
    outcomes: dict[str, str] = {}
    try:
        if use_tls:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server.ehlo()
            server.starttls(context=tls_context)
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30, context=tls_context)
            server.ehlo()
        server.login(prepared.sender, password)
        code, _ = server.mail(prepared.sender)
        if code != 250:
            outcomes.update((addr, _code_outcome(code)) for addr in recipients)
        else:
            for addr in recipients:
                code, _ = server.rcpt(addr)
                if code not in (250, 251, 252):
                    outcomes[addr] = _code_outcome(code)
                if code == 421:
                    # Server is closing: none of the previous RCPT successes
                    # reached DATA. Preserve definite rejections already seen.
                    outcomes.update((item, "temporary_failed") for item in recipients if item not in outcomes)
                    break
            pending = tuple(addr for addr in recipients if addr not in outcomes)
            if pending:
                submitted = True
                code, _ = server.data(prepared.mime_bytes)
                outcome = "accepted" if code == 250 else _code_outcome(code, submitted=True)
                outcomes.update((addr, outcome) for addr in pending)
    except Exception as exc:
        outcome = _exception_outcome(exc, submitted=submitted)
        outcomes.update((addr, outcome) for addr in recipients if addr not in outcomes)
        reason = "认证错误" if isinstance(exc, smtplib.SMTPAuthenticationError) else type(exc).__name__
        print(f"[邮件] SMTP 尝试结束（{reason}）；以逐收件人回执为准")
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
            finally:
                try:
                    server.close()
                except Exception:
                    pass
    return EmailDeliveryResult(configured=True, requested=recipients, **{
        kind: tuple(addr for addr in recipients if outcomes[addr] == kind)
        for kind in ("accepted", "temporary_failed", "permanent_failed", "unknown")
    })


def send_prepared_email(
    prepared: PreparedEmail, *, password: str,
    recipients: Optional[Sequence[str]] = None,
    custom_smtp_server: Optional[str] = None,
    custom_smtp_port: Optional[int] = None,
) -> EmailDeliveryResult:
    """One SMTP attempt; caller authorizes and persists each retry separately."""
    if recipients is not None and isinstance(recipients, str):
        raise ValueError("Recipients must be an envelope sequence")
    requested = prepared.recipients if recipients is None else tuple(recipients)
    if len(set(requested)) != len(requested) or not set(requested).issubset(prepared.recipients):
        raise ValueError("Recipients must be a unique subset of the original envelope")
    if not requested:
        return EmailDeliveryResult(configured=bool(password))
    if not password:
        return EmailDeliveryResult(configured=False, requested=requested, permanent_failed=requested)
    try:
        domain = prepared.sender.split("@")[-1].lower()
        if custom_smtp_server and custom_smtp_port:
            smtp_server, smtp_port = custom_smtp_server, int(custom_smtp_port)
            use_tls = smtp_port != 465
        else:
            config = SMTP_CONFIGS.get(domain, {"server": f"smtp.{domain}", "port": 587, "encryption": "TLS"})
            smtp_server, smtp_port = config["server"], config["port"]
            use_tls = config["encryption"] == "TLS"
        if not 1 <= smtp_port <= 65535:
            raise ValueError("Invalid SMTP port")
        tls_context = ssl.create_default_context()
    except Exception as exc:
        print(f"[邮件] SMTP 配置无效（{type(exc).__name__}）")
        return EmailDeliveryResult(configured=False, requested=requested, permanent_failed=requested)
    result = _deliver_once(
        smtp_server=smtp_server, smtp_port=smtp_port, use_tls=use_tls,
        tls_context=tls_context, prepared=prepared, password=password, recipients=requested,
    )
    if result.sent:
        print("[邮件] 邮件发送成功：全部请求收件人已被 SMTP 接受")
    elif result.partially_delivered:
        print("[邮件] 邮件部分投递：禁止整封重发")
    print(
        f"[邮件] SMTP 回执：接受={len(result.accepted)}，暂时失败={len(result.temporary_failed)}，"
        f"永久失败={len(result.permanent_failed)}，未知={len(result.unknown)}"
    )
    return result


def send_to_email(
    from_email: str, password: str, to_email: str, report_type: str,
    html_file_path: str, custom_smtp_server: Optional[str] = None,
    custom_smtp_port: Optional[int] = None, *, get_time_func: Optional[Callable] = None,
    subject_override: Optional[str] = None, sender_name_override: Optional[str] = None,
) -> EmailDeliveryResult:
    """Non-ledger convenience: prepare once and submit once, never auto-retry."""
    configured = bool(from_email and password and to_email)
    if not configured:
        return EmailDeliveryResult(configured=False)
    try:
        prepared = prepare_email(
            from_email, to_email, report_type, html_file_path,
            get_time_func=get_time_func, subject_override=subject_override,
            sender_name_override=sender_name_override,
        )
    except Exception as exc:
        print(f"[邮件] 报告准备失败（{type(exc).__name__}）")
        return EmailDeliveryResult(configured=True)
    return send_prepared_email(
        prepared, password=password, custom_smtp_server=custom_smtp_server,
        custom_smtp_port=custom_smtp_port,
    )
