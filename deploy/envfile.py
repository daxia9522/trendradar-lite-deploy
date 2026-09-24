"""Literal EnvironmentFile I/O shared by the native and Docker setup interfaces.

No shell evaluation. Unknown assignments and unedited lines survive a save.
Only the Docker double-quoted representation uses Compose's $$ escape.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path


class ConfigError(ValueError):
    """Safe, value-free error suitable for display beside secret fields."""


class ConcurrentEdit(ConfigError):
    pass


def _reject_symlinks(path: Path) -> None:
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ConfigError("配置路径或其父目录含符号链接，拒绝修改")


def snapshot(path: Path) -> tuple | None:
    _reject_symlinks(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError("配置路径不是普通文件")
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, path.read_bytes())


def _records(text: str, deployment: str = "linux") -> list[tuple[str, str | None, str | None]]:
    """Parse systemd's literal unquoted/single/double-quoted values, including continuations.

    Deliberately reject shell syntax such as export rather than pretending it ran.
    Diagnostics contain line numbers only, never source values.
    """
    result = []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        first = index + 1
        raw = lines[index]
        index += 1
        stripped = raw.lstrip()
        if not stripped.strip() or stripped.startswith(("#", ";")):
            result.append((raw, None, None))
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", stripped)
        if not match:
            raise ConfigError(f"环境文件第 {first} 行不是受支持的字面量赋值；请手工迁移")
        key = match[1]
        tail = stripped[match.end():].lstrip(" \t")
        quote = tail[0] if tail.startswith(("'", '"')) else None
        pos = 1 if quote else 0
        value = []
        trailing_unescaped = 0
        closed = not quote
        while True:
            if pos >= len(tail):
                if quote and not closed:
                    if index >= len(lines):
                        raise ConfigError(f"环境文件第 {first} 行引号未闭合")
                    extra = lines[index]
                    index += 1
                    raw += extra
                    tail += extra
                    continue
                break
            char = tail[pos]
            pos += 1
            if deployment == "docker" and quote == "'" and char == "\\" and pos < len(tail) and tail[pos] == "'":
                value.append("'")
                pos += 1
                continue
            if deployment == "docker" and not quote and char == "#" and value and value[-1].isspace():
                break
            if quote and char == quote:
                closed = True
                rest = tail[pos:].strip()
                if rest and not (deployment == "docker" and rest.startswith("#")):
                    raise ConfigError(f"环境文件第 {first} 行含不支持的引号后内容")
                break
            if char == "\\" and quote != "'":
                if pos >= len(tail):
                    # systemd drops an unquoted escape at EOF. Quoted input
                    # must still have a closing quote to be safely editable.
                    if quote:
                        raise ConfigError(f"环境文件第 {first} 行引号未闭合")
                    break
                next_char = tail[pos]
                pos += 1
                if next_char == "\n":
                    if index < len(lines):
                        extra = lines[index]
                        index += 1
                        raw += extra
                        tail += extra
                    continue
                if quote == '"' and next_char not in ('$', '`', '"', '\\'):
                    value.append("\\")
                value.append(next_char)
                # Escaped whitespace is significant even at the end of a value.
                trailing_unescaped = 0
            else:
                value.append(char)
                if not quote and char in " \t\r\n":
                    trailing_unescaped += 1
                else:
                    trailing_unescaped = 0
        if not quote and trailing_unescaped:
            del value[-trailing_unescaped:]
        decoded = "".join(value)
        if deployment == "docker" and quote == '"':
            decoded = decoded.replace("$$", "$")
        if "\0" in decoded:
            raise ConfigError(f"环境文件第 {first} 行含不支持的字符")
        result.append((raw, key, decoded))
    return result


def literal(value: str, deployment: str = "linux") -> str:
    if any(c in value for c in ("\r", "\n", "\0")):
        raise ConfigError("配置值不能包含换行或 NUL")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if deployment == "docker":
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Same-directory atomic replace, restrictive permissions before writing bytes."""
    _reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ConfigError("拒绝替换符号链接")
    owner = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if owner and (owner.st_uid, owner.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_backup(path: Path, content: bytes) -> Path:
    directory = path.parent / f".{path.name.lstrip('.')}.backups"
    if directory.is_symlink():
        raise ConfigError("备份目录不能是符号链接")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = path.stat()
    if (owner.st_uid, owner.st_gid) != (os.geteuid(), os.getegid()):
        os.chown(directory, owner.st_uid, owner.st_gid)
    os.chmod(directory, 0o700)
    fd, filename = tempfile.mkstemp(prefix="before-", dir=directory)
    with os.fdopen(fd, "wb") as stream:
        if (owner.st_uid, owner.st_gid) != (os.geteuid(), os.getegid()):
            os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return Path(filename)


class EnvDocument:
    def __init__(self, path: Path, deployment: str = "linux"):
        self.path = Path(path)
        self.deployment = deployment
        self.original = snapshot(self.path)
        self.content = self.original[-1] if self.original else b""
        try:
            self.records = _records(self.content.decode("utf-8"), deployment)
        except UnicodeError:
            raise ConfigError("环境文件必须使用 UTF-8") from None
        self.values = {key: value for _, key, value in self.records if key is not None}
        self.backup: Path | None = None

    def check_unchanged(self) -> None:
        if snapshot(self.path) != self.original:
            raise ConcurrentEdit("配置文件已被其他进程修改，未覆盖；请退出并重新打开")

    def render(self, updates: dict[str, str]) -> bytes:
        values = dict(self.values)
        values.update(updates)
        if not self.original:
            values.setdefault("STORAGE_BACKEND", "local")
        changed = {key for key in values if values[key] != self.values.get(key)}
        for key in values:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ConfigError("环境变量名称无效")
            if key in changed:
                literal(values[key], self.deployment)
        lines = []
        emitted = set()
        for raw, key, _value in self.records:
            if key not in changed:
                lines.append(raw)
            elif key not in emitted:
                lines.append(f"{key}={literal(values[key], self.deployment)}\n")
                emitted.add(key)
        for key in values:
            if key not in self.values:
                if lines and not lines[-1].endswith("\n"):
                    lines.append("\n")
                lines.append(f"{key}={literal(values[key], self.deployment)}\n")
        if self.deployment == "docker" and not self.content.startswith(b"# TrendRadar env format: docker\n"):
            lines.insert(0, "# TrendRadar env format: docker\n")
        return "".join(lines).encode("utf-8")

    def save(self, updates: dict[str, str]) -> bool:
        content = self.render(updates)
        self.check_unchanged()
        if self.original is not None and content == self.content:
            return False
        if self.original is not None:
            self.backup = private_backup(self.path, self.content)
        self.check_unchanged()
        atomic_write(self.path, content)
        return True

    def restore(self, written: bytes) -> None:
        """Never hide a failed rollback or overwrite a third-party edit."""
        current = snapshot(self.path)
        if not current or current[-1] != written:
            raise ConcurrentEdit("回滚失败：配置文件再次发生变化，保留现场及私有备份")
        if self.original is None:
            self.path.unlink()
        else:
            atomic_write(self.path, self.content)


def read_env(path: Path, deployment: str | None = None) -> dict[str, str]:
    if deployment is None:
        deployment = "docker" if path.exists() and path.read_bytes().startswith(b"# TrendRadar env format: docker\n") else "linux"
    return EnvDocument(path, deployment).values


def write_env(path: Path, values: dict[str, str], deployment: str = "linux") -> None:
    EnvDocument(path, deployment).save(values)
