#!/usr/bin/env python3
"""Native installer helpers; shell environment files are never sourced."""
from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import sys
from pathlib import Path

from envfile import ConfigError, atomic_write, read_env
from native_config import UnitTransaction, config_home


def launcher_content(app: Path, env: Path, units: Path, python: str) -> bytes:
    identity = hashlib.sha256(f"{app}\0{env}\0{units}".encode()).hexdigest()
    command = shlex.join([python, str(app / "deploy/configure.py"), "--output", str(env), "--unit-dir", str(units)])
    return f'#!/bin/sh\n# trendradar-lite owner:{identity}\nexec {command} "$@"\n'.encode()


def check_launcher(path: Path, content: bytes) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes().splitlines()[1:2] != content.splitlines()[1:2])):
        raise ConfigError("~/.local/bin/trendradar 已由其他程序或安装占用；拒绝覆盖")


def install_launcher(path: Path, content: bytes) -> None:
    check_launcher(path, content)
    if not path.exists() or path.read_bytes() != content:
        atomic_write(path, content, 0o755)


def remove_launcher(path: Path, content: bytes) -> None:
    if path.is_symlink():
        return
    if path.is_file() and path.read_bytes().splitlines()[1:2] == content.splitlines()[1:2]:
        path.unlink()


def unit_path(value: Path, executable: bool = False) -> str:
    text = str(value)
    if any(c in text for c in ("\n", "\r", "\0")):
        raise ConfigError("安装路径含不支持的字符")
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if executable:
        text = text.replace("$", "$$")
    return f'"{text}"'


def install_units(app: Path, env: Path, units: Path, runner=None) -> bool:
    from native_schedule import NativeSchedule, ScheduleError
    changes = {}
    timer_paths = [units / f"{name}.timer" for name in ("trendradar-lite", "trendradar-weekly")]
    is_new = not any(path.exists() for path in timer_paths)
    if is_new:
        values = read_env(env)
        required = {"TZ", "CRAWLER_MINUTE", "MORNING_PUSH_TIME", "NOON_PUSH_TIME", "EVENING_PUSH_TIME",
                    "DAILY_SUMMARY_TIME", "WEEKLY_WEEKDAY", "WEEKLY_HOUR", "WEEKLY_MINUTE"}
        if not all(values.get(key) for key in required):
            raise ConfigError("尚无 timer 且 env 时间配置不完整；请先 --configure 明确确认时间配置")
        try:
            changes.update(NativeSchedule(app, units, values).prepare(values, normalize=True))
        except ScheduleError as error:
            raise ConfigError(str(error)) from None
    elif not all(path.exists() for path in timer_paths):
        raise ConfigError("仅发现一个 timer；拒绝自动修复，请手工检查")
    for name in ("trendradar-lite", "trendradar-weekly"):
        path = units / f"{name}.service"
        if path.is_symlink():
            raise ConfigError("服务 unit 是符号链接；拒绝覆盖")
        if path.exists():
            text = path.read_text()
            if str(app) not in text or str(env) not in text:
                raise ConfigError("现有服务 unit 不属于此 checkout/config；拒绝覆盖")
            continue
        text = (app / f"deploy/systemd/{name}.service.in").read_text()
        text = text.replace("@APP_DIR@", unit_path(app)).replace("@ENV_FILE@", unit_path(env))
        text = text.replace("@PYTHON@", unit_path(app / ".venv/bin/python", executable=True))
        changes[path] = text.encode()
    UnitTransaction(changes, runner).commit()
    return is_new


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check-launcher", "install-launcher", "remove-launcher", "install-units", "doctor"))
    parser.add_argument("--app", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=config_home() / "trendradar-lite/env")
    parser.add_argument("--unit-dir", type=Path, default=config_home() / "systemd/user")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    app, env, units = args.app.resolve(), args.output.absolute(), args.unit_dir.absolute()
    launcher = Path.home() / ".local/bin/trendradar"
    content = launcher_content(app, env, units, args.python)
    try:
        if args.action == "check-launcher":
            check_launcher(launcher, content)
        elif args.action == "install-launcher":
            install_launcher(launcher, content)
        elif args.action == "remove-launcher":
            remove_launcher(launcher, content)
        elif args.action == "install-units":
            install_units(app, env, units)
        elif args.action == "doctor":
            environment = dict(os.environ)
            environment.update(read_env(env, deployment="linux"))
            os.chdir(app)
            os.execve(str(app / ".venv/bin/python"), [str(app / ".venv/bin/python"), "-m", "trendradar", "--doctor"], environment)
    except (ConfigError, OSError) as error:
        print(str(error) if isinstance(error, ConfigError) else "原生安装操作失败，请检查文件权限和 systemd 状态", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
