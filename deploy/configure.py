#!/usr/bin/env python3
"""Dependency-free native terminal menu and opt-in web/Docker setup."""

from __future__ import annotations

import argparse
import getpass
import html
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Also works when loaded by importlib and when invoked from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import ConfigError, EnvDocument, read_env, write_env


SECRET_FIELDS = {"EMAIL_PASSWORD", "AI_API_KEY"}
FIELDS = [
    ("EMAIL_FROM", "发件邮箱", True, "news@example.com"),
    ("EMAIL_PASSWORD", "邮箱密码或授权码", True, ""),
    ("EMAIL_TO", "收件邮箱", True, "多个地址用逗号分隔"),
    ("EMAIL_SMTP_SERVER", "SMTP 服务器", False, "留空则按发件邮箱自动识别"),
    ("EMAIL_SMTP_PORT", "SMTP 端口", False, "留空则按发件邮箱自动识别"),
    ("AI_ANALYSIS_ENABLED", "启用日报 AI 分析", False, "true / false"),
    ("AI_MODEL", "AI 模型", False, "openai/gpt-4o-mini"),
    ("AI_API_KEY", "AI API Key", False, ""),
    ("AI_API_KEY_FILE", "AI API Key 文件", False, "native Linux 可填写服务器上的密钥文件路径"),
    ("AI_API_BASE", "AI API 地址", False, "OpenAI 兼容接口可填写"),
    ("AI_FALLBACK_MODELS", "备用模型", False, "用逗号分隔"),
    ("AI_TIMEOUT", "AI 超时秒数", False, "正整数"),
    ("PLATFORMS_API_URL", "采集主接口", False, "HTTP(S) URL"),
    ("PLATFORMS_API_FALLBACK_URLS", "采集备用接口", False, "多个 HTTP(S) URL 用逗号分隔"),
    ("TZ", "时区", True, "Asia/Shanghai"),
    ("CRAWLER_MINUTE", "每小时采集分钟", False, "0-59"),
    ("MORNING_PUSH_TIME", "早间推送时间", False, "07:00"),
    ("NOON_PUSH_TIME", "午间推送时间", False, "12:00"),
    ("EVENING_PUSH_TIME", "傍晚推送时间", False, "18:00"),
    ("DAILY_SUMMARY_TIME", "全天汇总时间", False, "22:00"),
    ("WEEKLY_WEEKDAY", "周报星期", False, "0=周一，6=周日"),
    ("WEEKLY_HOUR", "周报小时", False, "0-23"),
    ("WEEKLY_MINUTE", "周报分钟", False, "0-59"),
]


def _fields_for(deployment: str) -> list[tuple[str, str, bool, str]]:
    """Return fields supported by the selected deployment target."""
    if deployment == "docker":
        return [field for field in FIELDS if field[0] != "AI_API_KEY_FILE"]
    return FIELDS


def render(
    values: dict[str, str],
    errors: list[str] | None = None,
    saved: bool = False,
    deployment: str = "linux",
) -> str:
    sections = {"邮件推送": [], "AI 分析": [], "执行时间": [], "高级配置": []}
    for key, label, required, hint in _fields_for(deployment):
        value = "" if key in SECRET_FIELDS else values.get(key, "")
        placeholder = "已保存，留空保持不变" if key in SECRET_FIELDS and values.get(key) else hint
        input_type = "password" if key in SECRET_FIELDS else "time" if key.endswith("_PUSH_TIME") or key == "DAILY_SUMMARY_TIME" else "text"
        if url_field_never_renders(key, value):
            # The raw value stays server-side only; blank submits keep the draft.
            value = ""
            placeholder = "已设置，含凭据，不回显；留空保持不变，输入 :clear 清空"
        field = (
            f'<label><span>{html.escape(label)}{" *" if required else ""}</span>'
            f'<input name="{key}" type="{input_type}" '
            f'value="{html.escape(value)}" placeholder="{html.escape(placeholder)}"'
            f'{" required" if required and not values.get(key) else ""}></label>'
        )
        section = _section_of(key)
        sections[section].append(field)
    section_html = "".join(
        f'<section><h2>{title}</h2>{"".join(fields)}</section>'
        for title, fields in sections.items()
    )
    notice = "<div class=ok>配置已保存，可以关闭此页面。</div>" if saved else ""
    error = ""
    if errors:
        error = "<div class=error>" + "<br>".join(html.escape(item) for item in errors) + "</div>"
    return f"""<!doctype html>
<html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TrendRadar Lite 配置</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:760px;margin:32px auto;padding:0 18px;color:#17202a;background:#f5f7fa}}
main{{background:white;padding:28px;border:1px solid #d9e0e7;border-radius:8px;box-shadow:0 2px 8px #0001}}
h1{{font-size:24px;margin-top:0}} h2{{font-size:17px;margin:0 0 13px}} p{{color:#52606d}} form{{display:grid;gap:24px}}
section{{display:grid;gap:13px}} label{{display:grid;gap:6px;font-weight:600}} input{{font:inherit;padding:10px;border:1px solid #b8c2cc;border-radius:5px}}
button{{font:inherit;padding:11px 16px;background:#1769aa;color:white;border:0;border-radius:5px;cursor:pointer}}
.ok{{padding:12px;background:#e3f6e8;color:#176b35;margin:14px 0}} .error{{padding:12px;background:#fde8e8;color:#a11;margin:14px 0}}
</style><main><h1>TrendRadar Lite 配置</h1>
<p>邮件配置为必填，SMTP 服务器和端口可留空自动识别。AI 分析可选，密码和密钥不会回显。</p>
{notice}{error}<form method="post">{section_html}<button type="submit">保存配置并继续安装</button></form></main></html>"""


def validate(values: dict[str, str], deployment: str = "linux") -> list[str]:
    fields = _fields_for(deployment)
    errors = [f"请填写：{label}" for key, label, required, _hint in fields if required and not values.get(key)]
    if values.get("AI_ANALYSIS_ENABLED", "false").lower() == "true":
        has_key = values.get("AI_API_KEY") or (
            deployment != "docker" and values.get("AI_API_KEY_FILE")
        )
        if not values.get("AI_MODEL") or not has_key:
            key_hint = "AI_API_KEY" if deployment == "docker" else "AI_API_KEY 或 AI_API_KEY_FILE"
            errors.append(f"启用 AI 分析时必须填写 AI_MODEL，以及 {key_hint}")
    if bool(values.get("EMAIL_SMTP_SERVER")) != bool(values.get("EMAIL_SMTP_PORT")):
        errors.append("EMAIL_SMTP_SERVER 和 EMAIL_SMTP_PORT 必须同时填写，或同时留空")
    for key, *_ in fields:
        errors.extend(validate_field(key, values.get(key, "")))
    times = [values.get(key) for key in TIME_FIELDS if values.get(key)]
    if len(times) != len(set(times)):
        errors.append("四个推送时间不能重复")
    if values.get("TIMEZONE") and values.get("TZ") and values["TIMEZONE"] != values["TZ"]:
        errors.append("TIMEZONE 与 TZ 冲突；请在时间菜单确认统一时区后保存")
    return errors


TIME_FIELDS = ("MORNING_PUSH_TIME", "NOON_PUSH_TIME", "EVENING_PUSH_TIME", "DAILY_SUMMARY_TIME")


def validate_field(key: str, value: str) -> list[str]:
    # Never interpolate entered values: these diagnostics may reach logs.
    if any(char in value for char in ("\n", "\r", "\0")):
        return [f"{key} 不能包含换行或 NUL"]
    if not value:
        return []
    ranges = {
        "EMAIL_SMTP_PORT": (1, 65535),
        "CRAWLER_MINUTE": (0, 59),
        "WEEKLY_WEEKDAY": (0, 6),
        "WEEKLY_HOUR": (0, 23),
        "WEEKLY_MINUTE": (0, 59),
        "AI_TIMEOUT": (1, 2147483647),
    }
    if key in ranges:
        minimum, maximum = ranges[key]
        try:
            number = int(value)
        except ValueError:
            return [f"{key} 必须是整数"]
        if not minimum <= number <= maximum:
            return [f"{key} 必须在 {minimum}-{maximum} 之间"]
    if key in TIME_FIELDS and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        return [f"{key} 必须使用 HH:MM 格式"]
    if key == "AI_ANALYSIS_ENABLED" and value.lower() not in ("true", "false"):
        return ["AI_ANALYSIS_ENABLED 必须是 true 或 false"]
    if key in ("AI_API_BASE", "PLATFORMS_API_URL", "PLATFORMS_API_FALLBACK_URLS"):
        urls = value.split(",") if key.endswith("FALLBACK_URLS") else [value]
        for url in urls:
            try:
                parsed = urllib.parse.urlsplit(url.strip())
                valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
                parsed.port
            except ValueError:
                valid = False
            if not valid:
                return [f"{key} 必须是有效的 HTTP(S) 地址"]
    if key in ("TZ", "TIMEZONE"):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            return [f"{key} 必须是有效的 IANA 时区（例如 Asia/Shanghai）"]
    return []


def _write_timer(path: Path, description: str, calendars: list[str]) -> None:
    calendars = list(dict.fromkeys(calendars))
    lines = [
        "[Unit]",
        f"Description={description}",
        "",
        "[Timer]",
        *(f"OnCalendar={value}" for value in calendars),
        "Persistent=true",
        "AccuracySec=1min",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_systemd_timer(path: Path, values: dict[str, str]) -> None:
    crawler_minute = int(values.get("CRAWLER_MINUTE") or "5")
    push_times = [
        values.get("MORNING_PUSH_TIME", "07:00"),
        values.get("NOON_PUSH_TIME", "12:00"),
        values.get("EVENING_PUSH_TIME", "18:00"),
        values.get("DAILY_SUMMARY_TIME", "22:00"),
    ]
    timezone = values.get("TZ") or "Asia/Shanghai"
    calendars = [f"*-*-* *:{crawler_minute:02d}:00 {timezone}"]
    calendars.extend(f"*-*-* {value}:00 {timezone}" for value in push_times)
    _write_timer(
        path,
        "Run TrendRadar Lite for hourly collection and configured delivery times",
        calendars,
    )


def write_systemd_weekly_timer(path: Path, values: dict[str, str]) -> None:
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    weekday = weekdays[int(values.get("WEEKLY_WEEKDAY") or "6")]
    hour = int(values.get("WEEKLY_HOUR") or "12")
    minute = int(values.get("WEEKLY_MINUTE") or "30")
    _write_timer(
        path,
        f"Run TrendRadar Lite weekly report on {weekday}",
        [f"{weekday} *-*-* {hour:02d}:{minute:02d}:00 {values.get('TZ') or 'Asia/Shanghai'}"],
    )


def _section_of(key: str) -> str:
    if key in ("AI_TIMEOUT", "PLATFORMS_API_URL", "PLATFORMS_API_FALLBACK_URLS"):
        return "高级配置"
    if key.startswith("EMAIL_"):
        return "邮件推送"
    if key.startswith("AI_"):
        return "AI 分析"
    return "执行时间"


def _prompt_field(
    key: str, label: str, required: bool, hint: str, current: dict[str, str]
) -> str:
    """Prompt one field on a terminal; empty input keeps the current value."""
    is_secret = key in SECRET_FIELDS
    has_current = bool(current.get(key))
    if is_secret and has_current:
        note = "已保存，回车保持不变"
    elif is_secret:
        note = hint
    elif has_current:
        note = f"当前 {current[key]}，回车保持不变"
    else:
        note = hint
    label_text = f"{label}{' *' if required else ''}"
    prompt = f"  {label_text}" + (f"（{note}）" if note else "") + ": "
    entered = (getpass.getpass(prompt) if is_secret else input(prompt)).strip()
    return entered if entered else current.get(key, "")


def configure_docker_terminal(output: Path, deployment: str = "docker") -> None:
    """Interactive terminal setup wizard; a TTY-friendly alternative to the web page."""
    document = EnvDocument(output, deployment)
    current = dict(document.values)
    fields = _fields_for(deployment)
    print("TrendRadar Lite 终端配置向导", flush=True)
    print("邮件为必填（标 *），SMTP 可留空自动识别；AI 可选。直接回车沿用已保存的值。", flush=True)
    while True:
        submitted = dict(current)
        last_section = ""
        for key, label, required, hint in fields:
            section = _section_of(key)
            if section != last_section:
                print(f"\n[{section}]", flush=True)
                last_section = section
            submitted[key] = _prompt_field(key, label, required, hint, current)
        errors = validate(submitted, deployment=deployment)
        if not errors:
            document.save(submitted)
            print("\n配置已保存，安装将继续。", flush=True)
            return
        print("\n配置有误，请修正后重新填写：", flush=True)
        for item in errors:
            print(f"  - {item}", flush=True)
        print("（已填写的值会保留，回车即可沿用）", flush=True)
        current = submitted


MENU_SECTIONS = {
    "1": ("邮件推送", [field[0] for field in FIELDS if field[0].startswith("EMAIL_")]),
    "2": ("AI 模型与接口", ["AI_ANALYSIS_ENABLED", "AI_MODEL", "AI_API_KEY", "AI_API_KEY_FILE", "AI_API_BASE", "AI_FALLBACK_MODELS"]),
    "3": ("采集与推送时间", ["CRAWLER_MINUTE", *TIME_FIELDS, "WEEKLY_WEEKDAY", "WEEKLY_TIME", "TZ"]),
    "4": ("高级配置", ["AI_TIMEOUT", "PLATFORMS_API_URL", "PLATFORMS_API_FALLBACK_URLS"]),
}
FIELD_MAP = {field[0]: field for field in FIELDS}
FIELD_MAP["WEEKLY_TIME"] = ("WEEKLY_TIME", "周报时间", False, "HH:MM")


def display_value(key: str, value: str) -> str:
    if key in SECRET_FIELDS:
        return "<已设置，隐藏>" if value else "<未设置>"
    if key == "AI_ANALYSIS_ENABLED" and value.lower() in ("true", "false"):
        return "已启用" if value.lower() == "true" else "已停用"
    if key == "WEEKLY_WEEKDAY" and value in tuple(str(day) for day in range(7)):
        return f"周{'一二三四五六日'[int(value)]}（{value}）"
    if "URL" in key or key == "AI_API_BASE":
        # URLs may embed bearer credentials in their authority or query.
        try:
            if any(urllib.parse.urlsplit(part.strip()).query or "@" in urllib.parse.urlsplit(part.strip()).netloc
                   for part in value.split(",") if part.strip()):
                return "<已设置，含敏感 URL，隐藏>"
        except ValueError:
            return "<已设置，URL 无法解析，隐藏>"
    if not value and key in ("AI_TIMEOUT", "PLATFORMS_API_URL", "PLATFORMS_API_FALLBACK_URLS"):
        return "<使用程序配置>"
    return value or "<未设置>"


CREDENTIAL_URL_FIELDS = ("AI_API_BASE", "PLATFORMS_API_URL", "PLATFORMS_API_FALLBACK_URLS")
CLEAR_SENTINEL = ":clear"


def carries_credentials(value: str) -> bool:
    """True when a URL value embeds secrets in userinfo or query parameters."""
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            split = urllib.parse.urlsplit(part)
        except ValueError:
            return True
        if split.query or "@" in split.netloc:
            return True
    return False


def url_field_never_renders(key: str, value: str) -> bool:
    """Public URL inputs may carry bearer credentials; never echo those back."""
    return key in CREDENTIAL_URL_FIELDS and carries_credentials(value)


def changes_between(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    return {key: after.get(key, "") for key in set(before) | set(after)
            if before.get(key, "") != after.get(key, "")}


def print_changes(before: dict[str, str], after: dict[str, str]) -> None:
    diff = changes_between(before, after)
    print("\n[待保存变更]", flush=True)
    if not diff:
        print("没有待保存变更。", flush=True)
    for key in sorted(diff):
        if key in SECRET_FIELDS:
            operation = "将清空" if not diff[key] else "将替换" if before.get(key) else "将新增"
            print(f"  {key}: {operation}（内容隐藏）", flush=True)
        else:
            print(f"  {key}: {display_value(key, before.get(key, ''))} → {display_value(key, diff[key])}", flush=True)


def _native_value(key: str, values: dict[str, str]) -> str:
    if key == "WEEKLY_TIME":
        hour, minute = values.get("WEEKLY_HOUR", ""), values.get("WEEKLY_MINUTE", "")
        return f"{int(hour):02d}:{int(minute):02d}" if hour.isdigit() and minute.isdigit() else ""
    return values.get(key, "")


def edit_native_field(key: str, values: dict[str, str]) -> None:
    _, label, required, hint = FIELD_MAP[key]
    current = _native_value(key, values)
    while True:
        note = "1 启用 / 2 禁用" if key == "AI_ANALYSIS_ENABLED" else hint
        prompt = f"{label} [{display_value(key, current)}]（{note}；回车保留，:cancel 取消"
        prompt += "）: " if required else "，:clear 清空）: "
        entered = getpass.getpass(prompt) if key in SECRET_FIELDS else input(prompt)
        if key not in SECRET_FIELDS:
            entered = entered.strip()
        if entered in ("", ":cancel"):
            return
        if entered == ":clear":
            if required:
                print("必填字段不能清空。", flush=True)
                continue
            entered = ""
        if key == "AI_ANALYSIS_ENABLED":
            entered = {"1": "true", "2": "false"}.get(entered, entered.lower())
        errors = validate_field("MORNING_PUSH_TIME" if key == "WEEKLY_TIME" else key, entered)
        if key in TIME_FIELDS and entered and any(values.get(other) == entered for other in TIME_FIELDS if other != key):
            errors.append("四个推送时间不能重复")
        if errors:
            print("\n".join(errors), flush=True)
            continue
        if key == "WEEKLY_TIME":
            parts = entered.split(":") if entered else ("", "")
            values["WEEKLY_HOUR"], values["WEEKLY_MINUTE"] = (str(int(v)) if v else "" for v in parts)
        else:
            values[key] = entered
        if key == "TZ" and values.get("TIMEZONE") and values["TIMEZONE"] != entered:
            print("旧 TIMEZONE 会覆盖 TZ。统一后才能应用时间配置。", flush=True)
            if input("同时将 TIMEZONE 统一为此时区？[y/N]: ").strip().lower() == "y":
                values["TIMEZONE"] = entered
        return


def configure_terminal(output: Path, deployment: str = "linux", *, application=None,
                       install: bool = False) -> bool:
    if deployment == "docker":
        configure_docker_terminal(output, deployment)
        return True
    from native_config import NativeApplication, SCHEDULE_KEYS
    app = application or NativeApplication(output, install=install)
    before = app.document.values
    values = dict(before)
    if app.document.original is None:
        # Defaults are a proposal in memory only, never a pre-created config file.
        values.update(read_env(app.app_dir / ".env.example"))
    normalize = False
    print("TrendRadar Lite 原生终端配置（仅保存时写磁盘；不会自动测试邮件、AI 或采集）", flush=True)
    print(f"配置文件：{app.document.path}", flush=True)
    for warning in app.schedule.warnings:
        print(f"警告：{warning}", flush=True)
    while True:
        print("\n1 邮件推送\n2 AI 模型与接口\n3 采集与推送时间\n4 高级配置\n5 查看待保存变更\ns 保存并应用\nq 放弃修改并退出", flush=True)
        choice = input("选择: ").strip().lower()
        if choice in MENU_SECTIONS:
            title, keys = MENU_SECTIONS[choice]
            if choice == "3":
                for line in app.schedule.describe():
                    print(line, flush=True)
            while True:
                print(f"\n[{title}]", flush=True)
                if choice == "3":
                    print("n 将检测到的时间补全为明确配置（保存时需确认规范化）；0 返回", flush=True)
                for index, key in enumerate(keys, 1):
                    pending = " [待保存]" if _native_value(key, values) != _native_value(key, before) else ""
                    print(f"{index} {FIELD_MAP[key][1]}: {display_value(key, _native_value(key, values))}{pending}", flush=True)
                selection = input("字段编号（0 返回）: ").strip().lower()
                if selection in ("0", "", "q"):
                    break
                if selection == "n" and choice == "3":
                    if not app.schedule.supported:
                        print("此自定义时间配置不支持自动迁移，请手工处理。", flush=True)
                        continue
                    for key, value in app.schedule.suggested.items():
                        if key in SCHEDULE_KEYS and not values.get(key):
                            values[key] = value
                    if values.get("TIMEZONE") and values.get("TZ") != values["TIMEZONE"]:
                        values["TZ"] = values["TIMEZONE"]
                    normalize = True
                    print("已加入待保存变更，尚未写磁盘。", flush=True)
                elif selection.isdigit() and 1 <= int(selection) <= len(keys):
                    edit_native_field(keys[int(selection) - 1], values)
                else:
                    print("请输入有效字段编号。", flush=True)
        elif choice == "5":
            print_changes(before, values)
            if normalize:
                print("另有待应用变更：将两个 timer 规范化为明确的 TZ 和推送分钟。", flush=True)
        elif choice == "q":
            if changes_between(before, values) or normalize:
                if input("放弃全部未保存修改？[y/N]: ").strip().lower() != "y":
                    continue
            print("已退出，未保存修改。", flush=True)
            return False
        elif choice == "s":
            schedule_changed = normalize or any(key in changes_between(before, values) for key in SCHEDULE_KEYS)
            check_values = dict(values)
            if schedule_changed:
                for key, suggestion in app.schedule.suggested.items():
                    if key not in values or (not values[key] and not before.get(key)):
                        check_values[key] = suggestion
            # Existing missing TZ is legacy state, not permission to silently normalize it.
            if not schedule_changed:
                check_values.setdefault("TZ", values.get("TIMEZONE") or "Asia/Shanghai")
                check_values.pop("TIMEZONE", None)
            errors = validate(check_values)
            if errors:
                print("\n".join(errors), flush=True)
                continue
            if schedule_changed and app.schedule.warnings:
                print("时间配置将规范化：四次推送窗口收敛到指定分钟，timer 显式使用 TZ。", flush=True)
                for warning in app.schedule.warnings:
                    print(f"警告：{warning}", flush=True)
                if input("确认规范化时间配置？[y/N]: ").strip().lower() != "y":
                    continue
                normalize = True
            if normalize:
                for key in SCHEDULE_KEYS:
                    if key in check_values:
                        values[key] = check_values[key]
            print_changes(before, values)
            if input("确认保存并应用？[y/N]: ").strip().lower() != "y":
                continue
            try:
                result = app.save(values, normalize=normalize)
            except (ConfigError, OSError) as error:
                print(str(error) if isinstance(error, ConfigError) else "保存失败，未完成应用；请检查文件权限与私有备份。", flush=True)
                continue
            print(result, flush=True)
            return True
        else:
            print("请输入有效菜单选项。", flush=True)


def detect_ssh_target(user: str, host: str, ssh_port: int) -> tuple[str, str, int]:
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if not host and len(connection) == 4:
        host = connection[2]
    if ssh_port == 0 and len(connection) == 4 and connection[3].isdigit():
        ssh_port = int(connection[3])
    login_user = user or os.environ.get("SUDO_USER") or os.environ.get("USER") or getpass.getuser()
    return login_user, host or "server", ssh_port or 22


def ssh_tunnel_command(user: str, host: str, ssh_port: int, setup_port: int) -> str:
    port_option = f" -p {ssh_port}" if ssh_port != 22 else ""
    return f"ssh{port_option} -L {setup_port}:127.0.0.1:{setup_port} {user}@{host}"


def serve(
    output: Path,
    host: str,
    port: int,
    public_port: int,
    ssh_user: str,
    ssh_host: str,
    ssh_port: int,
    deployment: str,
    application=None,
) -> None:
    document = application.document if application else EnvDocument(output, deployment)
    current = dict(document.values)
    if application and document.original is None:
        current.update(read_env(application.app_dir / ".env.example"))
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def send_page(self, body: str, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def page(self, values, errors=None, saved=False):
            page = render(values, errors, saved, deployment)
            if application and application.schedule.warnings:
                notice = "<p>时间配置警告：" + "；".join(html.escape(x) for x in application.schedule.warnings) + "</p>"
                notice += '<label><input type="checkbox" name="_normalize_schedule" value="yes">确认规范化时间配置（将推送窗口收敛到指定分钟）</label>'
                page = page.replace('<form method="post">', '<form method="post">' + notice)
            return page

        def do_GET(self) -> None:
            self.send_page(self.page(current))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            submitted = dict(current)
            submitted.update({
                key: form.get(key, [""])[0] if key in SECRET_FIELDS else form.get(key, [""])[0].strip()
                for key, *_rest in _fields_for(deployment)
            })
            for key in CREDENTIAL_URL_FIELDS:
                if key not in form:
                    continue
                entered = form[key][0].strip()
                if entered == CLEAR_SENTINEL:
                    submitted[key] = ""
                elif not entered and carries_credentials(current.get(key, "")):
                    # Redacted input submitted unchanged: keep the server-side draft.
                    submitted[key] = current[key]
            for key in SECRET_FIELDS:
                if key in submitted and not submitted[key] and current.get(key):
                    submitted[key] = current[key]
            normalize = form.get("_normalize_schedule") == ["yes"]
            check_values = dict(submitted)
            if application:
                from native_config import SCHEDULE_KEYS
                if normalize:
                    for key, suggestion in application.schedule.suggested.items():
                        if not submitted.get(key) and not document.values.get(key):
                            submitted[key] = suggestion
                    if submitted.get("TIMEZONE") and submitted.get("TZ"):
                        submitted["TIMEZONE"] = submitted["TZ"]
                    check_values = dict(submitted)
                elif not any(submitted.get(key, "") != document.values.get(key, "") for key in SCHEDULE_KEYS):
                    if not check_values.get("TZ"):
                        check_values["TZ"] = submitted.get("TIMEZONE") or "Asia/Shanghai"
                    check_values.pop("TIMEZONE", None)
            errors = validate(check_values, deployment=deployment)
            if errors:
                self.send_page(self.page(submitted, errors), 400)
                return
            try:
                if application:
                    application.save(submitted, normalize=normalize)
                else:
                    document.save(submitted)
            except (ConfigError, OSError) as error:
                message = str(error) if isinstance(error, ConfigError) else "保存或应用失败，请检查文件权限及私有备份"
                self.send_page(self.page(submitted, [message]), 409)
                return
            self.send_page(self.page(submitted, saved=True))
            done.set()

    server = HTTPServer((host, port), Handler)
    setup_port = public_port or server.server_port
    user, target_host, target_port = detect_ssh_target(ssh_user, ssh_host, ssh_port)
    command = ssh_tunnel_command(user, target_host, target_port, setup_port)
    print(f"配置页面：http://127.0.0.1:{setup_port}/", flush=True)
    print("远程 VPS 请在本机终端直接复制执行：", flush=True)
    print(command, flush=True)
    print("保持该终端运行，再用本机浏览器打开配置页面。", flush=True)
    while not done.is_set():
        server.handle_request()
    server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="TrendRadar Lite configuration wizard")
    from native_config import config_home
    parser.add_argument("--output", type=Path, default=config_home() / "trendradar-lite/env")
    parser.add_argument("--unit-dir", type=Path, default=config_home() / "systemd/user")
    parser.add_argument("--install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-port", type=int, default=0)
    parser.add_argument("--ssh-user", default="")
    parser.add_argument("--ssh-host", default="")
    parser.add_argument("--ssh-port", type=int, default=0)
    parser.add_argument("--render-systemd-timer", type=Path)
    parser.add_argument("--render-systemd-weekly-timer", type=Path)
    parser.add_argument("--deployment", choices=("linux", "docker"), default="linux")
    parser.add_argument("--runtime-config", action="store_true", help="Docker runtime/env 分组配置（字面量格式）")
    parser.add_argument(
        "--mode",
        choices=("auto", "terminal", "web"),
        default="auto",
        help="Linux 默认终端菜单；Docker auto 保留旧行为；web 显式开启网页",
    )
    parser.add_argument("--terminal", action="store_true", help="等价于 --mode terminal")
    parser.add_argument("--web", action="store_true", help="等价于 --mode web")
    args = parser.parse_args()
    if args.runtime_config:
        if args.deployment != "docker":
            parser.error("--runtime-config 仅用于 --deployment docker")
        from docker.docker_configure import main as docker_runtime_main
        return docker_runtime_main(args)
    if args.render_systemd_timer:
        write_systemd_timer(args.render_systemd_timer, read_env(args.output))
        return 0
    if args.render_systemd_weekly_timer:
        write_systemd_weekly_timer(args.render_systemd_weekly_timer, read_env(args.output))
        return 0
    mode = args.mode
    if args.terminal:
        mode = "terminal"
    if args.web:
        mode = "web"
    if mode == "auto":
        mode = "terminal" if args.deployment == "linux" or sys.stdin.isatty() else "web"
    if mode == "terminal":
        if args.deployment == "linux" and not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("原生菜单需要交互式 TTY；未写入配置。请在终端运行 trendradar，或显式使用 --web。", file=sys.stderr)
            return 2
        try:
            if args.deployment == "linux":
                from native_config import NativeApplication
                app = NativeApplication(args.output, unit_dir=args.unit_dir, install=args.install)
                saved = configure_terminal(args.output, application=app)
            else:
                saved = configure_terminal(args.output, args.deployment)
        except (EOFError, KeyboardInterrupt):
            print(
                "终端配置不可用：标准输入已结束或无法交互。"
                "已取消，未保存尚未确认的修改。",
                file=sys.stderr,
                flush=True,
            )
            return 2
        except (ConfigError, OSError) as error:
            print(str(error) if isinstance(error, ConfigError) else "无法读取配置文件", file=sys.stderr)
            return 2
        return 0 if saved else 2
    try:
        application = None
        if args.deployment == "linux":
            from native_config import NativeApplication
            application = NativeApplication(args.output, unit_dir=args.unit_dir, install=args.install)
        serve(
            args.output,
            args.host,
            args.port,
            args.public_port,
            args.ssh_user,
            args.ssh_host,
            args.ssh_port,
            args.deployment,
            application=application,
        )
    except (EOFError, KeyboardInterrupt, ConfigError, OSError) as error:
        print(str(error) if isinstance(error, ConfigError) else "网页配置已中止，未完成保存", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
