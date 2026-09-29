#!/usr/bin/env python3
"""Remove report units only after a confirmed stop; never restart a report timer."""
from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

from envfile import ConfigError, atomic_write, snapshot
from native_config import ApplyError, TIMER_NAMES, UnitTransaction, _PROPERTIES, _same_state

REPORT_NAMES = tuple(name.removesuffix(".timer") + suffix
                     for name in TIMER_NAMES for suffix in (".service", ".timer"))
DISABLED = ("disabled", "static", "", "not-found")


class RemovalTransaction(UnitTransaction):
    def properties(self, path):
        # show may return 1 for a missing unit. Only accept a complete, known
        # not-found state, never a bus failure or an empty/partial response.
        proc = self.call("show", path.name, "--property=LoadState," + _PROPERTIES, check=False)
        props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        if not {"ActiveState", "UnitFileState", "FragmentPath", "DropInPaths"} <= props.keys():
            raise ApplyError("timer 状态输出不完整；未继续卸载")
        missing = (props.get("LoadState") == "not-found"
                   and props.get("ActiveState") == "inactive"
                   and props.get("SubState") == "dead"
                   and props.get("UnitFileState") in ("", "not-found")
                   and props.get("FragmentPath") == "" and props.get("DropInPaths") == "")
        if proc.returncode and not (proc.returncode == 1 and missing):
            raise ApplyError("无法确认 timer 状态；未继续卸载")
        return props

    def verify_states(self):
        for path in self.timers:
            props = self.properties(path)
            if not _same_state(self.states[path.name], (props.get("ActiveState"), props.get("UnitFileState"))):
                raise ApplyError("未卸载的 timer 启用或运行状态出现变化")


def stopped(transaction, units):
    for name in TIMER_NAMES:
        props = transaction.properties(units / name)
        if props.get("ActiveState") != "inactive" or props.get("UnitFileState") not in DISABLED:
            raise ApplyError("报告 timer 停止/禁用状态未确认；未继续删除或重载")
        if props.get("DropInPaths"):
            raise ApplyError("报告 timer 出现有效 drop-in；未继续卸载")
        fragment = props.get("FragmentPath", "")
        if fragment and Path(fragment).resolve() != (units / name).resolve():
            raise ApplyError("报告 timer 实际加载路径已变化；未继续卸载")


def remove_reports(units: Path, runner=None):
    paths = [units / name for name in REPORT_NAMES]
    # Include missing manager-visible timers, not just files found on disk.
    # The existing gate still applies to every reload, including recovery.
    transaction = RemovalTransaction({}, runner, guard_paths=[*paths, units / "trendradar-r2-backup.timer"])
    transaction.preflight(initial=True)
    transaction.check()
    original = dict(transaction.before)
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in paths if original[path] is not None}
    targets = []
    for name in TIMER_NAMES:
        props = transaction.properties(units / name)
        if original[units / name] is not None or props.get("LoadState") != "not-found":
            targets.append(name)
    if targets:
        # A nonzero return/timeout may already have changed state. Do not delete,
        # reload, or try to start a persistent report timer as compensation.
        transaction.call("disable", "--no-reload", "--now", *targets)
    stopped(transaction, units)
    # Reports are deliberately stopped, not state-preserved. Keep checking that
    # explicitly; unchanged backup timers retain UnitTransaction's calendar gate.
    for name in TIMER_NAMES:
        transaction.timers.pop(units / name)
        transaction.states.pop(name)
    transaction.preflight()
    transaction.check()
    stopped(transaction, units)
    removed = []
    try:
        for path in paths:
            transaction.check()
            if original[path] is not None:
                removed.append(path)
                path.unlink()
                transaction.before[path] = None
        transaction.preflight()
        stopped(transaction, units)
        transaction.check()
        transaction.reload()
        transaction.verify_states()
        stopped(transaction, units)
    except (ConfigError, OSError) as error:
        failures = []
        for path in reversed(removed):
            try:
                current = snapshot(path)
                if current is None:
                    atomic_write(path, original[path][-1], modes[path])
                elif current != original[path]:
                    raise ApplyError("报告 unit 已被其他进程修改")
                transaction.before[path] = snapshot(path)
            except (ConfigError, OSError):
                failures.append(path.name)
        if transaction.reloaded and not failures:
            try:
                transaction.preflight()
                stopped(transaction, units)
                transaction.check()
                transaction.reload()
                transaction.verify_states()
                stopped(transaction, units)
            except (ConfigError, OSError):
                failures.append("systemd 状态")
        detail = "；恢复未确认：" + "、".join(failures) if failures else ""
        raise ApplyError("报告 unit 卸载失败；未自动重启报告 timer" + detail) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        remove_reports(args.unit_dir.absolute())
    except (ConfigError, OSError) as error:
        print(str(error) if isinstance(error, ConfigError) else "报告 unit 卸载失败，请检查文件权限和 systemd 状态",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
