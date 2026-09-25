"""Docker setup: literal runtime documents, guarded legacy migration, no service control.

Compose contract: setup runs root and mounts only the project at /setup;
--runtime-config selects this adapter. TRENDRADAR_UID/GID identify
non-root service ownership. setup needs no build stanza: install builds the
trendradar service explicitly; configure only consumes its existing image.
"""
from __future__ import annotations

import html
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configure as shared
from envfile import ConfigError, EnvDocument, private_backup, read_env, snapshot
from deploy.docker.runtime_config import RuntimeConfigError, is_application_key, parse_runtime_env, validate_runtime_values

DOCKER_MARKER = b"# TrendRadar env format: docker\n"
MIGRATION_ERROR = "旧 .env 含不支持的 Compose 插值或转义；未迁移。请先将应用参数改为明确字面量（美元符号用 $$ 或单引号），再重试。"
def application_key(key: str) -> bool:
    return key != "AI_API_KEY_FILE" and is_application_key(key)


def validate(values: dict[str, str], *, require_mail: bool = True) -> list[str]:
    checked = dict(values)
    checked.setdefault("TZ", values.get("TIMEZONE") or "Asia/Shanghai")
    errors = shared.validate(checked, "docker") if require_mail else []
    try:
        validate_runtime_values(values)
    except RuntimeConfigError as error:
        errors.append(str(error))
    return errors
SECTIONS = {choice: (title, [key for key in keys if key != "AI_API_KEY_FILE"])
            for choice, (title, keys) in shared.MENU_SECTIONS.items()}


class FieldPolicy(NamedTuple):
    label: str
    sensitive: bool = True


# This is Docker-local display metadata, not a mutation of the native UI's
# global secret set. Only explicitly reviewed public fields may display values.
# Prefix-supported future fields remain sensitive until classified here.
PUBLIC_FIELDS = frozenset({
    "EMAIL_FROM", "EMAIL_TO", "EMAIL_SMTP_SERVER", "EMAIL_SMTP_PORT",
    "AI_ANALYSIS_ENABLED", "AI_MODEL", "AI_API_BASE", "AI_FALLBACK_MODELS", "AI_TIMEOUT",
    "PLATFORMS_API_URL", "PLATFORMS_API_FALLBACK_URLS", "TZ", "TIMEZONE",
    "CRAWLER_MINUTE", "MORNING_PUSH_TIME", "NOON_PUSH_TIME", "EVENING_PUSH_TIME",
    "DAILY_SUMMARY_TIME", "WEEKLY_WEEKDAY", "WEEKLY_HOUR", "WEEKLY_MINUTE", "WEEKLY_TIME",
    "S3_BUCKET_NAME", "S3_ENDPOINT_URL", "S3_REGION", "STORAGE_BACKEND",
    "DEBUG", "CONFIG_PATH", "FREQUENCY_WORDS_PATH", "SORT_BY_POSITION_FIRST",
    "MAX_NEWS_PER_KEYWORD", "LOCAL_RETENTION_DAYS", "REMOTE_RETENTION_DAYS",
    "PULL_ENABLED", "PULL_DAYS", "DOCKER_CONTAINER", "SCHEDULE_ENABLED",
    "SCHEDULER_POLL_SECONDS", "SCHEDULER_MAX_ATTEMPTS", "STORAGE_TXT_ENABLED", "STORAGE_HTML_ENABLED",
})
FIELD_POLICIES = {key: FieldPolicy(shared.FIELD_MAP[key][1] if key in shared.FIELD_MAP else key,
                                  sensitive=key not in PUBLIC_FIELDS)
                  for key in PUBLIC_FIELDS | (set(shared.FIELD_MAP) - {"AI_API_KEY_FILE"}) |
                  {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"}}


def field_policy(key: str) -> FieldPolicy:
    return FIELD_POLICIES.get(key, FieldPolicy(key))


def display_value(key: str, value: str) -> str:
    if field_policy(key).sensitive:
        return "<已设置，隐藏>" if value else "<未设置>"
    return shared.display_value(key, value)


def change_lines(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines = []
    for key, value in sorted(shared.changes_between(before, after).items()):
        if field_policy(key).sensitive:
            operation = "将清空" if not value else "将替换" if before.get(key) else "将新增"
            shown = f"{operation}（内容隐藏）"
        else:
            shown = f"{display_value(key, before.get(key, ''))} → {display_value(key, value)}"
        lines.append(f"{key}: {shown}")
    return lines


def print_changes(before: dict[str, str], after: dict[str, str]) -> None:
    print("\n[待保存变更]", flush=True)
    print("\n".join(change_lines(before, after)) or "没有待保存变更。", flush=True)


def edit_field(key: str, values: dict[str, str]) -> None:
    if not field_policy(key).sensitive:
        shared.edit_native_field(key, values)
        return
    # Required secrets may be cleared as a draft, but full validation prevents
    # saving until required credentials are supplied again.
    while True:
        entered = shared.getpass.getpass(f"{field_policy(key).label} [{display_value(key, values.get(key, ''))}]"
                                        "（回车保留，:cancel 取消，:clear 清空）: ")
        if entered in ("", ":cancel"):
            return
        value = "" if entered == ":clear" else entered
        errors = shared.validate_field(key, value)
        if errors:
            print("\n".join(errors), flush=True)
            continue
        values[key] = value
        return


def target_identity() -> tuple[int, int]:
    values = (os.environ.get("TRENDRADAR_UID", str(os.getuid() or 1000)),
              os.environ.get("TRENDRADAR_GID", str(os.getgid() or 1000)))
    if any(not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 2147483647 for value in values):
        raise ConfigError("TRENDRADAR_UID/GID 必须是非零有效数字；常驻服务不能使用 root")
    return tuple(int(value) for value in values)


def private_runtime(path: Path, identity: tuple[int, int], *, apply: bool = True) -> None:
    # Refuse every unsafe path before changing metadata. The pre-save check must
    # not chmod the observed file: ctime participates in concurrent-edit checks.
    snapshot(path)
    directory = path.parent
    targets = [directory] if directory.exists() else []
    if path.exists():
        targets.append(path)
    backup_dir = directory / ".env.backups"
    if backup_dir.is_symlink() or (backup_dir.exists() and not backup_dir.is_dir()):
        raise ConfigError("备份目录不是安全目录")
    if backup_dir.exists():
        targets.append(backup_dir)
        for item in backup_dir.iterdir():
            if item.is_symlink() or not item.is_file() or item.stat().st_nlink != 1:
                raise ConfigError("备份路径不是普通文件")
            targets.append(item)
    if apply:
        for item in targets:
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
            if os.geteuid() == 0:
                os.chown(item, *identity)


def legacy_values(path: Path) -> dict[str, str]:
    """Decode only application values, without evaluating Compose or host variables."""
    document = EnvDocument(path, "docker")
    values = {}
    for raw, key, value in document.records:
        if key is None or not application_key(key):
            continue
        tail = raw.lstrip().split("=", 1)[1].lstrip()
        if not tail.startswith("'"):
            # EnvDocument is a literal reader, not Compose's interpolation engine.
            if re.search(r"\$(?:[A-Za-z_]|\{)", tail.replace("$$", "")):
                raise ConfigError(MIGRATION_ERROR)
            if tail.startswith('"'):
                if any(char not in ('\\', '"') for char in re.findall(r'\\(.)', tail)):
                    raise ConfigError(MIGRATION_ERROR)
            else:
                if "\\" in tail:
                    raise ConfigError(MIGRATION_ERROR)
                value = value.replace("$$", "$")
        if any(char in value for char in ("\n", "\r", "\0")):
            raise ConfigError(MIGRATION_ERROR)
        values[key] = value
    return values


def validate_document(document: EnvDocument, values: dict[str, str]) -> None:
    """Check the exact bytes that will become authoritative, not just form fields."""
    try:
        parse_runtime_env(document.render(values))
    except RuntimeConfigError as error:
        # runtime_config uses deploy.envfile; UI uses bare envfile. Do not rely
        # on those two import identities having the same ConfigError class.
        raise ConfigError(str(error)) from None


def backup_legacy(source: EnvDocument, identity: tuple[int, int]) -> None:
    source.check_unchanged()
    backup = private_backup(source.path, source.content)
    if os.geteuid() == 0:
        os.chown(backup.parent, *identity)
        os.chown(backup, *identity)
    source.check_unchanged()


def secure_legacy(path: Path, identity: tuple[int, int]) -> None:
    os.chmod(path, 0o600)
    if os.geteuid() == 0:
        os.chown(path, *identity)


class DockerApplication:
    def __init__(self, output: Path, *, legacy: Path | None = None, defaults: Path | None = None):
        self.identity = target_identity()
        self.output = Path(output)
        self.legacy_document = None
        # Existing docker-marked documents retain their explicit old representation.
        original = snapshot(self.output)
        file_format = "docker" if original and original[-1].startswith(DOCKER_MARKER) else "linux"
        self.document = EnvDocument(self.output, file_format)
        self.values = dict(self.document.values)
        if self.document.original is None:
            if legacy is not None and legacy.exists():
                source = EnvDocument(legacy, "docker")
                proposed = legacy_values(legacy)
                if proposed:
                    source.check_unchanged()
                    self.legacy_document = source
                    self.values.update(proposed)
                    print("检测到旧 .env：应用参数已读入待保存草稿；确认保存前不会迁移或创建备份。", flush=True)
            if not self.values and defaults is not None:
                self.values.update({key: value for key, value in read_env(defaults).items() if application_key(key)})

    def save(self, values: dict[str, str]) -> str:
        errors = validate(values)
        if errors:
            raise ConfigError("\n".join(errors))
        validate_document(self.document, values)
        private_runtime(self.output, self.identity, apply=False)
        self.document.check_unchanged()
        if self.legacy_document is not None:
            backup_legacy(self.legacy_document, self.identity)
        self.document.save(values)
        private_runtime(self.output, self.identity)
        if self.legacy_document is not None:
            secure_legacy(self.legacy_document.path, self.identity)
            print("旧 .env 私有备份已保留；runtime/env 现为运行配置权威。", flush=True)
        return "配置已保存至 runtime/env；未拉取/构建镜像，也未启动或重建服务。"


def configure_terminal(application: DockerApplication) -> bool:
    before = application.document.values
    values = dict(application.values)
    print("TrendRadar Lite Docker 分组配置（仅保存时写磁盘；不会测试邮件、AI 或采集）", flush=True)
    print(f"配置文件：{application.output}", flush=True)
    while True:
        print("\n1 邮件推送\n2 AI 模型与接口\n3 采集与推送时间\n4 高级配置\n5 查看待保存变更\ns 保存配置\nq 放弃修改并退出", flush=True)
        choice = input("选择: ").strip().lower()
        if choice in SECTIONS:
            title, keys = SECTIONS[choice]
            while True:
                print(f"\n[{title}]", flush=True)
                for index, key in enumerate(keys, 1):
                    current = shared._native_value(key, values)
                    pending = " [待保存]" if current != shared._native_value(key, before) else ""
                    print(f"{index} {field_policy(key).label}: {display_value(key, current)}{pending}", flush=True)
                selection = input("字段编号（0 返回）: ").strip().lower()
                if selection in ("0", "", "q"):
                    break
                if selection.isdigit() and 1 <= int(selection) <= len(keys):
                    edit_field(keys[int(selection) - 1], values)
                else:
                    print("请输入有效字段编号。", flush=True)
        elif choice == "5":
            print_changes(before, values)
        elif choice == "q":
            if shared.changes_between(before, values) and input("放弃全部未保存修改？[y/N]: ").strip().lower() != "y":
                continue
            print("已取消，未保存修改；安装不会继续启动服务。", flush=True)
            return False
        elif choice == "s":
            errors = validate(values)
            if errors:
                print("\n".join(errors), flush=True)
                continue
            print_changes(before, values)
            if input("确认保存配置？[y/N]: ").strip().lower() != "y":
                continue
            try:
                print(application.save(values), flush=True)
            except (ConfigError, OSError) as error:
                print(str(error) if isinstance(error, ConfigError) else "保存失败，请检查文件权限与私有备份。", flush=True)
                continue
            return True
        else:
            print("请输入有效菜单选项。", flush=True)


def render(values, errors=None, saved=False, *, before=None, secret_edits=None) -> str:
    before = before or {}
    secret_edits = secret_edits or {}
    # Never pass a sensitive value into the shared HTML template, including
    # fields that a future shared release adds without classifying as secret.
    public = {key: value if not field_policy(key).sensitive else "" for key, value in values.items()}
    page = shared.render(public, errors, saved, "docker")
    # Reuse the shared web template, replacing just its two legacy weekly inputs.
    weekly = html.escape(shared._native_value("WEEKLY_TIME", values), quote=True)
    page = re.sub(r'<label><span>周报小时</span>.*?</label>',
                  f'<label><span>周报时间</span><input name="WEEKLY_TIME" type="time" value="{weekly}"></label>', page)
    page = re.sub(r'<label><span>周报分钟</span>.*?</label>', '', page)
    page = page.replace("保存配置并继续安装", "保存配置")
    controls = '<button name="_action" value="preview">查看待保存变更</button><button name="_action" value="cancel" formnovalidate>取消并退出</button>'
    page = page.replace('</form>', controls + '</form>')
    rendered_keys = {key for key, *_ in shared._fields_for("docker")}
    secret_keys = {key for key in rendered_keys | set(values) if field_policy(key).sensitive and application_key(key)}
    extra = []
    for key in sorted(secret_keys):
        if saved:
            note = "已保存，留空保持不变" if values.get(key) else "未设置"
        elif secret_edits.get(key) == "clear":
            note = "待清空（未保存）；留空保持此草稿"
        elif key in secret_edits or values.get(key, "") != before.get(key, ""):
            note = "待保存秘密草稿；留空保持此草稿"
        else:
            note = "已保存，留空保持不变" if values.get(key) else "未设置；留空不变"
        safe_key = html.escape(key, quote=True)
        field = (f'<input name="{safe_key}" type="password" value="" '
                 f'placeholder="{html.escape(note, quote=True)}" autocomplete="new-password">')
        if key in rendered_keys:
            page = re.sub(r'<input name="' + re.escape(key) + r'"[^>]*>', lambda _match: field, page)
        else:
            extra.append(f'<label><span>{html.escape(field_policy(key).label)}</span>{field}</label>')
        extra.append(f'<label><input type="checkbox" name="_clear_{safe_key}" value="yes">'
                     f'清空 {html.escape(field_policy(key).label)}（保留为待保存操作）</label>')
    page = page.replace('</form>', ''.join(extra) + '</form>')
    page = page.replace('<form method="post">', '<p>密码留空保留服务器端草稿；校验或保存失败不会丢弃新密码。'
                        '勾选清空会清除草稿中的值；取消或关闭本次配置服务将丢弃未保存草稿。</p><form method="post">')
    return page


def serve(application: DockerApplication, args) -> bool:
    current = dict(application.values)
    before = application.document.values
    secret_edits = {}
    done = threading.Event()
    saved = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def send_page(self, body, status=200):
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            self.send_page(render(current, before=before, secret_edits=secret_edits))

        def do_POST(self):
            nonlocal saved, current
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 65536:
                    raise ValueError
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            except (ValueError, UnicodeError):
                self.send_page(render(current, ["请求格式无效"], before=before, secret_edits=secret_edits), 400)
                return
            action = form.get("_action", ["save"])[0]
            if action == "cancel":
                self.send_page("已取消，未保存修改；安装不会继续启动服务。")
                done.set()
                return
            submitted = dict(current)
            for key, *_ in shared._fields_for("docker"):
                if key in ("WEEKLY_HOUR", "WEEKLY_MINUTE"):
                    continue
                if not field_policy(key).sensitive and key in form:
                    submitted[key] = form[key][0].strip()
            secret_keys = {key for key in set(current) | {key for key, *_ in shared._fields_for("docker")}
                           if application_key(key) and field_policy(key).sensitive}
            for key in secret_keys:
                value = form.get(key, [""])[0]
                if form.get(f"_clear_{key}") == ["yes"]:
                    submitted[key] = ""
                    secret_edits[key] = "clear"
                elif value:
                    submitted[key] = value
                    secret_edits[key] = "replace"
            weekly = form.get("WEEKLY_TIME", [""])[0].strip()
            errors = shared.validate_field("MORNING_PUSH_TIME", weekly)
            if not errors:
                parts = weekly.split(":") if weekly else ("", "")
                submitted["WEEKLY_HOUR"], submitted["WEEKLY_MINUTE"] = (str(int(v)) if v else "" for v in parts)
            # The server owns the unconfirmed draft. Commit it to memory before
            # any validation/save branch so a blank retry never restores an old
            # credential. It is never written to disk without a valid save.
            current = submitted
            errors.extend(validate(submitted))
            if errors:
                self.send_page(render(current, errors, before=before, secret_edits=secret_edits), 400)
                return
            if action == "preview":
                # Keep secrets server-side; never place them in HTML inputs or previews.
                lines = change_lines(before, current)
                notice = '<pre>[待保存变更]\n' + html.escape("\n".join(lines) or "没有待保存变更。") + '</pre>'
                self.send_page(render(current, before=before, secret_edits=secret_edits).replace('<form method="post">', notice + '<form method="post">'))
                return
            try:
                application.save(submitted)
            except (ConfigError, OSError) as error:
                message = str(error) if isinstance(error, ConfigError) else "保存失败，请检查权限与私有备份"
                self.send_page(render(current, [message], before=before, secret_edits=secret_edits), 409)
                return
            saved = True
            self.send_page(render(submitted, saved=True))
            done.set()

    server = HTTPServer((args.host, args.port), Handler)
    public_port = args.public_port or server.server_port
    user, host, port = shared.detect_ssh_target(args.ssh_user, args.ssh_host, args.ssh_port)
    print(f"配置页面：http://127.0.0.1:{public_port}/", flush=True)
    print(shared.ssh_tunnel_command(user, host, port, public_port), flush=True)
    try:
        while not done.is_set():
            server.handle_request()
    finally:
        server.server_close()
    return saved


def main(args) -> int:
    try:
        application = DockerApplication(args.output, legacy=args.output.parent.parent / ".env",
                                        defaults=args.output.parent.parent / ".env.example")
        mode = "web" if args.web else "terminal" if args.terminal else args.mode
        if mode == "auto":
            mode = "terminal" if sys.stdin.isatty() and sys.stdout.isatty() else "web"
        saved = configure_terminal(application) if mode == "terminal" else serve(application, args)
        return 0 if saved else 2
    except (EOFError, KeyboardInterrupt):
        print("已取消：输入结束或操作中止；未保存尚未确认的修改，安装不会启动服务。", file=sys.stderr)
        return 2
    except (ConfigError, OSError) as error:
        print(str(error) if isinstance(error, ConfigError) else "配置失败，请检查路径、所有权与私有备份。", file=sys.stderr)
        return 2
