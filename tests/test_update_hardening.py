"""Updater safety using fake Git/installer only; never mutate a real Git repo."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

FAKE_GIT = r'''
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
assert args[:1] == ["-C"], args
root = Path(args[1])
args = args[2:]
with (root / "calls.jsonl").open("a") as output:
    output.write(json.dumps(["git", *args]) + "\n")
state = json.loads((root / "fixture.json").read_text())
command = args[0]
if command == "status":
    assert args == ["status", "--short", "--untracked-files=no"], args
    if state.get("status_error"):
        sys.exit(128)
    print(state.get("dirty", ""), end="")
elif command == "rev-parse":
    assert args == ["rev-parse", "--verify", "@{upstream}"], args
    if state.get("no_upstream"):
        sys.exit(128)
    print(state["upstream"])
elif command == "fetch":
    assert args == ["fetch", "--no-recurse-submodules"], args
    if state.get("fetch_error"):
        sys.exit(1)
elif command == "merge":
    assert args == ["merge", "--ff-only", "--no-autostash", "@{upstream}"], args
    # A synthetic single-parent commit graph models whether a fast-forward is
    # possible. No real Git binary, repository, config, refs or history are used.
    cursor = state["upstream"]
    while cursor and cursor != state["head"]:
        cursor = state["parents"].get(cursor)
    if cursor != state["head"]:
        print("fatal: Not possible to fast-forward, aborting.", file=sys.stderr)
        sys.exit(128)
    state["head"] = state["upstream"]
    (root / "fixture.json").write_text(json.dumps(state))
else:
    raise AssertionError("Unexpected Git command (pull/rebase/stash forbidden): " + repr(args))
'''

FAKE_INSTALLER = r'''
import json
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[2]
with (root / "calls.jsonl").open("a") as output:
    output.write(json.dumps(["install", *sys.argv[1:]]) + "\n")
'''


class UpdateHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="updater fixture ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "deploy/linux").mkdir(parents=True)
        (self.root / ".git").mkdir()
        (self.root / "bin").mkdir()
        shutil.copyfile(ROOT / "deploy/linux/update.sh", self.root / "deploy/linux/update.sh")
        for path, body in ((self.root / "bin/git", FAKE_GIT),
                           (self.root / "deploy/linux/install.sh", FAKE_INSTALLER)):
            path.write_text(f"#!{sys.executable}\n" + body)
            path.chmod(0o700)
        self.state = {"head": "old", "upstream": "new", "parents": {"new": "old", "old": None}}
        self.env = {
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.root),
            # Explicitly model a user who has configured pull to rebase. The
            # updater must not invoke pull at all, regardless of these settings.
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "pull.rebase",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_CONFIG_KEY_1": "pull.ff",
            "GIT_CONFIG_VALUE_1": "false",
        }

    def run_update(self):
        (self.root / "fixture.json").write_text(json.dumps(self.state))
        result = subprocess.run(
            ["bash", str(self.root / "deploy/linux/update.sh")],
            env=self.env, cwd=self.root, text=True, capture_output=True, timeout=15,
        )
        log = self.root / "calls.jsonl"
        self.calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.after = json.loads((self.root / "fixture.json").read_text())
        return result

    def assert_no_install(self):
        self.assertFalse(any(call[0] == "install" for call in self.calls), self.calls)

    def test_fast_forward_uses_fetch_merge_despite_pull_rebase_configuration(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls, [
            ["git", "status", "--short", "--untracked-files=no"],
            ["git", "rev-parse", "--verify", "@{upstream}"],
            ["git", "fetch", "--no-recurse-submodules"],
            ["git", "merge", "--ff-only", "--no-autostash", "@{upstream}"],
            ["install", "--no-enable"],
        ])
        self.assertEqual(self.after["head"], "new")
        self.assertIn("timer enablement was preserved", result.stdout)

    def test_non_fast_forward_is_rejected_without_install_or_history_rewrite(self):
        self.state.update(head="local", parents={"local": "old", "new": "old", "old": None})
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not possible to fast-forward", result.stderr)
        self.assertEqual(self.after["head"], "local")
        self.assert_no_install()
        self.assertEqual(self.calls[-1], ["git", "merge", "--ff-only", "--no-autostash", "@{upstream}"])

    def test_already_current_upstream_is_safe(self):
        self.state["head"] = "new"
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.after["head"], "new")
        self.assertEqual(self.calls[-1], ["install", "--no-enable"])

    def test_tracked_local_changes_abort_before_fetch(self):
        self.state["dirty"] = " M config/config.yaml\n"
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local changes", result.stderr)
        self.assertEqual(len(self.calls), 1)
        self.assert_no_install()

    def test_status_failure_is_not_mistaken_for_clean_tree(self):
        self.state["status_error"] = True
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot inspect", result.stderr)
        self.assertEqual(len(self.calls), 1)
        self.assert_no_install()

    def test_missing_upstream_aborts_before_fetch(self):
        self.state["no_upstream"] = True
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[1] for call in self.calls], ["status", "rev-parse"])
        self.assert_no_install()

    def test_failed_fetch_aborts_before_merge(self):
        self.state["fetch_error"] = True
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[1] for call in self.calls], ["status", "rev-parse", "fetch"])
        self.assertEqual(self.after["head"], "old")
        self.assert_no_install()

    def test_non_clone_aborts_without_git(self):
        (self.root / ".git").rmdir()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a Git clone", result.stderr)
        self.assertEqual(self.calls, [])
        self.assert_no_install()


if __name__ == "__main__":
    unittest.main()
