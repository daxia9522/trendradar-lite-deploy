#!/usr/bin/env python3
"""Short-lived Docker setup operations; never call Docker, systemd, mail or AI.

Invoke via: compose run --rm --pull never --no-deps --entrypoint python setup
            deploy/docker/manage.py {prepare,check,persist-identity} --root /setup
init-volume runs via the isolated volume-init service; its path is fixed.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docker_configure import DockerApplication, private_runtime, target_identity, validate, validate_document
from envfile import ConfigError, EnvDocument, atomic_write


def persist_identity(root: Path, identity: tuple[int, int]) -> None:
    path = root / ".env"
    document = EnvDocument(path, "docker")
    if document.original is None:
        # EnvDocument's new application document adds STORAGE_BACKEND by default;
        # deployment metadata must contain no app defaults before the user saves.
        content = ("# Deployment settings only; application configuration: runtime/env\n"
                   f'TRENDRADAR_UID="{identity[0]}"\nTRENDRADAR_GID="{identity[1]}"\n')
        atomic_write(path, content.encode("utf-8"))
        if os.geteuid() == 0:
            os.chown(path, *identity)
        return
    # Preserve deployment expressions and unrelated legacy lines verbatim.
    # Existing root .env must never be sourced by a shell.
    document.save({"TRENDRADAR_UID": str(identity[0]), "TRENDRADAR_GID": str(identity[1])})
    os.chmod(path, 0o600)
    if os.geteuid() == 0:
        os.chown(path, *identity)
        if document.backup:
            os.chown(document.backup.parent, *identity)
            os.chown(document.backup, *identity)


def prepare(root: Path, identity: tuple[int, int]) -> None:
    # Read-only path preflight, not migration or identity persistence. Incomplete
    # old application values are intentionally left for the menu to correct.
    EnvDocument(root / ".env", "docker")
    private_runtime(root / "runtime/env", identity, apply=False)


def check(root: Path) -> None:
    app = DockerApplication(root / "runtime/env")
    if app.document.original is None:
        raise ConfigError("runtime/env 尚未保存；不会启动服务")
    errors = validate(app.document.values)
    if errors:
        raise ConfigError("\n".join(errors))
    validate_document(app.document, app.document.values)
    private_runtime(root / "runtime/env", app.identity, apply=False)
    print("runtime/env 校验通过（未输出任何配置值）。", flush=True)


def init_volume(identity: tuple[int, int], *, output: Path = Path("/app/output")) -> None:
    # 'output' is injected only by unit tests. No CLI path override is provided.
    if output.is_symlink() or not output.is_dir():
        raise ConfigError("输出卷挂载无效；拒绝修改权限")
    # Preflight the whole tree before mutating. Symlink targets are never followed;
    # hardlinks could alias files outside output, so reject those too.
    paths = [output]
    for directory, dirs, files in os.walk(output, followlinks=False):
        paths.extend(Path(directory) / name for name in dirs + files)
    for path in paths:
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ConfigError("输出卷含链接或特殊文件；拒绝自动修改权限")
    for path in paths:
        os.chown(path, *identity, follow_symlinks=False)
    print("输出卷所有权已初始化为非 root 服务用户。", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check", "persist-identity", "init-volume"))
    parser.add_argument("--root", type=Path, default=Path("/setup"))
    args = parser.parse_args(argv)
    try:
        identity = target_identity()
        if args.command == "prepare":
            prepare(args.root, identity)
        elif args.command == "check":
            check(args.root)
        elif args.command == "persist-identity":
            # Defend this public command too: no metadata writes without a
            # previously saved and valid authoritative runtime document.
            check(args.root)
            persist_identity(args.root, identity)
            private_runtime(args.root / "runtime/env", identity)
        else:
            init_volume(identity)
    except (ConfigError, OSError) as error:
        print(str(error) if isinstance(error, ConfigError) else "Docker 配置操作失败；请检查路径、文件权限和私有备份。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
