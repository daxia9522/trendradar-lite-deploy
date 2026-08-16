# coding=utf-8
"""邮件发送器（精简版：仅 Email）。"""

import smtplib
import ssl
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Callable, Optional

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
) -> bool:
    """发送 HTML 报告邮件。"""
    try:
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

        recipients = [addr.strip() for addr in to_email.split(",") if addr.strip()]
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
        server.send_message(msg)
        server.quit()
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
