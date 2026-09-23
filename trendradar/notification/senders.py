# coding=utf-8
"""邮件发送器（精简版：仅 Email）。"""

import smtplib
import ssl
import re
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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
    # 465 为隐式 SSL；587 为 STARTTLS（TLS）
    "aliyun.com": {"server": "smtp.aliyun.com", "port": 465, "encryption": "SSL"},
    "yandex.com": {"server": "smtp.yandex.com", "port": 465, "encryption": "SSL"},
    "icloud.com": {"server": "smtp.mail.me.com", "port": 587, "encryption": "TLS"},
    "vip.163.com": {"server": "smtp.vip.163.com", "port": 465, "encryption": "SSL"},
}

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

# 首次失败后的重试间隔（秒）。授权码与网络正常时投递只需数秒，
# 这里只覆盖 SMTP 侧偶发的瞬时拒绝，因此间隔保持短促。
SEND_RETRY_DELAYS = (3, 15)


def _is_retryable_smtp_error(exc: Exception) -> bool:
    # 保留供应商兼容策略：认证错误仍重试（包括 535），不泛化到其他 5xx。
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return True
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # 抛异常表示本次未投递；仅全部为临时拒收时才重试整个信封。
        return bool(exc.recipients) and all(
            400 <= code < 500 for code, _ in exc.recipients.values()
        )
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, (smtplib.SMTPServerDisconnected, OSError))


def _deliver_once(
    *,
    smtp_server: str,
    smtp_port: int,
    use_tls: bool,
    tls_context: ssl.SSLContext,
    from_email: str,
    password: str,
    recipients: List[str],
    msg: MIMEMultipart,
) -> Dict[str, Tuple[int, bytes]]:
    """建立连接、登录并投递；返回拒收字典，异常交由调用方判定。

    每次尝试都新建连接并确保关闭，避免复用半开连接。
    """
    server = None
    try:
        if use_tls:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server.ehlo()
            server.starttls(context=tls_context)
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(
                smtp_server,
                smtp_port,
                timeout=30,
                context=tls_context,
            )
            server.ehlo()
        server.login(from_email, password)
        return server.send_message(msg, from_addr=from_email, to_addrs=recipients)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def send_to_email(
    from_email: str,
    password: str,
    to_email: str,
    report_type: str,
    html_file_path: str,
    custom_smtp_server: Optional[str] = None,
    custom_smtp_port: Optional[int] = None,
    *,
    get_time_func: Callable = None,
    subject_override: Optional[str] = None,
    sender_name_override: Optional[str] = None,
    on_partial_delivery: Optional[Callable[[], None]] = None,
) -> bool:
    """发送 HTML 报告；仅所有收件人均被 SMTP 接受时返回 True。

    部分拒收返回 False 并停止重试；回调用于上层记录已发生投递，阻止整批补发。
    """
    try:
        if not _EMAIL_RE.fullmatch(from_email.strip()):
            print("错误：发件人地址无效")
            return False

        recipients = [addr.strip() for addr in to_email.split(",") if addr.strip()]
        if not recipients or any(not _EMAIL_RE.fullmatch(addr) for addr in recipients):
            print("错误：收件人地址无效或为空")
            return False

        if not html_file_path or not Path(html_file_path).exists():
            print("错误：HTML 文件不存在或未提供")
            return False

        source_path = Path(html_file_path)
        print("已加载 HTML 报告文件")
        with open(source_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        domain = from_email.split("@")[-1].lower()

        if custom_smtp_server and custom_smtp_port:
            smtp_server = custom_smtp_server
            smtp_port = int(custom_smtp_port)
            use_tls = smtp_port != 465
        elif domain in SMTP_CONFIGS:
            config = SMTP_CONFIGS[domain]
            smtp_server = config["server"]
            smtp_port = config["port"]
            use_tls = config["encryption"] == "TLS"
        else:
            print("未识别的邮箱服务商，使用通用 SMTP 配置")
            smtp_server = f"smtp.{domain}"
            smtp_port = 587
            use_tls = True

        msg = MIMEMultipart("alternative")
        sender_name = sender_name_override or "TrendRadar"
        msg["From"] = formataddr((sender_name, from_email))

        msg["To"] = recipients[0] if len(recipients) == 1 else ", ".join(recipients)

        now = get_time_func() if get_time_func else datetime.now()
        subject = subject_override or f"TrendRadar 热点分析报告 - {report_type} - {now.strftime('%m月%d日 %H:%M')}"
        msg["Subject"] = Header(subject, "utf-8")
        msg["MIME-Version"] = "1.0"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        report_label = sender_name_override or "TrendRadar"
        text_content = f"""
{report_label} 热点分析报告
========================
报告类型：{report_type}
生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}

请使用支持HTML的邮件客户端查看完整报告内容。
        """
        msg.attach(MIMEText(text_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        print("正在发送邮件...")

        # 显式校验 SMTP 证书（Python 默认 starttls/SMTP_SSL context 不校验）
        tls_context = ssl.create_default_context()

        attempts = len(SEND_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                refused = _deliver_once(
                    smtp_server=smtp_server,
                    smtp_port=smtp_port,
                    use_tls=use_tls,
                    tls_context=tls_context,
                    from_email=from_email,
                    password=password,
                    recipients=recipients,
                    msg=msg,
                )
            except (smtplib.SMTPException, OSError) as exc:
                if not _is_retryable_smtp_error(exc) or attempt == attempts:
                    raise
                delay = SEND_RETRY_DELAYS[attempt - 1]
                # 只记录异常类型：SMTP 异常文本可能包含邮箱地址。
                print(
                    f"邮件发送第 {attempt}/{attempts} 次失败"
                    f"（{type(exc).__name__}），{delay} 秒后重试"
                )
                time.sleep(delay)
            else:
                if refused:
                    # 正常返回非空字典表示已部分投递，不能再按整批失败重试。
                    print(
                        f"邮件部分投递：{len(refused)} 个收件人被拒绝；"
                        "其余已被 SMTP 接受，不自动重试"
                    )
                    if on_partial_delivery is not None:
                        on_partial_delivery()
                    return False
                print(f"邮件发送成功 [{report_type}]")
                return True

    except smtplib.SMTPAuthenticationError:
        print("邮件发送失败：认证错误，请检查邮箱和密码/授权码")
        return False
    except smtplib.SMTPRecipientsRefused:
        print("邮件发送失败：收件人地址被拒绝")
        return False
    except smtplib.SMTPSenderRefused:
        print("邮件发送失败：发件人地址被拒绝")
        return False
    except smtplib.SMTPDataError:
        print("邮件发送失败：邮件数据错误")
        return False
    except smtplib.SMTPConnectError:
        print(f"邮件发送失败：无法连接到 SMTP 服务器")
        return False
    except smtplib.SMTPServerDisconnected:
        print("邮件发送失败：服务器意外断开连接，请检查网络或稍后重试")
        return False
    except Exception as e:
        print(f"邮件发送失败 [{report_type}]：{type(e).__name__}")
        return False
