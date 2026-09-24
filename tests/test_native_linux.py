"""Native shell lifecycle in a fake HOME, checkout, interpreter and systemctl."""
import importlib.util
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lifecycle_configure", ROOT / "deploy/configure.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)
from envfile import read_env, write_env
from native_install import install_launcher, launcher_content


class LinuxLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.app = self.root / "checkout with spaces"
        shutil.copytree(ROOT / "deploy", self.app / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "config", self.app / "config")
        shutil.copy(ROOT / ".env.example", self.app / ".env.example")
        shutil.copy(ROOT / "requirements.txt", self.app / "requirements.txt")
        self.config = self.root / "custom xdg"
        self.envpath = self.config / "trendradar-lite/env"
        self.units = self.config / "systemd/user"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.environment = {"PATH": str(self.bin) + os.pathsep + os.defpath,
                            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
                            "PYTHON_BIN": sys.executable, "LANG": "C.UTF-8",
                            "FAKE_LOG": str(self.log), "FAKE_UNITS": str(self.units)}
        fake_systemctl = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["systemctl", *sys.argv[1:]]) + "\\n")
if "show" in sys.argv:
    name = sys.argv[sys.argv.index("show") + 1]
    print("ActiveState=inactive\\nUnitFileState=disabled\\nDropInPaths=")
    print("FragmentPath=" + str(Path(os.environ["FAKE_UNITS"]) / name))
'''
        (self.bin / "systemctl").write_text(fake_systemctl)
        (self.bin / "systemctl").chmod(0o755)
        python = self.app / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["fake-python", *sys.argv[1:]]) + "\\n")
if "--doctor" in sys.argv:
    Path(os.environ["FAKE_LOG"] + ".doctor").write_text(os.environ.get("EMAIL_PASSWORD", ""))
''')
        python.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def populated_env(self):
        values = read_env(self.app / ".env.example")
        values.update(EMAIL_FROM="sender@example.com", EMAIL_TO="reader@example.com", EMAIL_PASSWORD="password", AI_MODEL="original/model")
        write_env(self.envpath, values)
        return values

    def run_script(self, name, *args, answers=None):
        command = ["bash", str(self.app / f"deploy/linux/{name}.sh"), *args]
        if answers is None:
            return subprocess.run(command, cwd=self.root, env=self.environment, input="", capture_output=True, text=True, timeout=15)
        return self.run_tty(command, answers)

    def run_tty(self, command, answers):
        master, slave = pty.openpty()
        process = subprocess.Popen(command, cwd=self.root, env=self.environment, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        transcript = bytearray()
        # Answer one prompt at a time: getpass may flush queued terminal input.
        pending = list(answers)
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    transcript.extend(chunk)
                    if pending and pending[0][0].encode() in transcript:
                        _, reply = pending.pop(0)
                        os.write(master, reply.encode())
                        # Only subsequent output can satisfy the next prompt.
                        transcript.extend(b"\n[answered]\n")
                        if pending:
                            # Consume matching old prompts by tracking the cursor below.
                            break
                if process.poll() is not None:
                    break
            # General prompt driver uses a separate fresh buffer for each subsequent answer.
            buffer = bytearray()
            while time.monotonic() < deadline and process.poll() is None:
                if not select.select([master], [], [], 0.05)[0]:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                transcript.extend(chunk)
                buffer.extend(chunk)
                if pending and pending[0][0].encode() in buffer:
                    _, reply = pending.pop(0)
                    os.write(master, reply.encode())
                    buffer.clear()
            if process.poll() is None:
                process.kill()
            code = process.wait(timeout=2)
            self.assertFalse(pending, transcript.decode(errors="replace"))
            return subprocess.CompletedProcess(command, code, transcript.decode(errors="replace"), "")
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_non_tty_new_install_aborts_without_any_artifact_or_pip(self):
        result = self.run_script("install")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("TTY", result.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse((self.home / ".local").exists())
        self.assertFalse((self.app / "output").exists())
        self.assertEqual(self.calls(), [])

    def test_fresh_cancel_and_eof_do_not_continue_install(self):
        for replies in ([('选择:', 'q\n'), ('放弃全部', 'y\n')], [('选择:', '\x04')]):
            result = self.run_script("install", answers=replies)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse(self.config.exists())
            self.assertFalse((self.home / ".local").exists())
            self.assertFalse((self.app / "output").exists())
            self.assertEqual(self.calls(), [])

    def test_configure_flag_is_menu_only_without_pip_or_timer_calls(self):
        self.populated_env()
        old = self.envpath.read_bytes()
        result = self.run_script("install", "--configure", answers=[("选择:", "q\n")])
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(self.envpath.read_bytes(), old)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.app / "output").exists())

    def test_install_registers_path_safe_launcher_and_maintenance_does_not_enable(self):
        self.populated_env()
        first = self.run_script("install", "--no-enable")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        launcher = self.home / ".local/bin/trendradar"
        self.assertTrue(launcher.exists())
        self.assertIn("not on PATH", first.stdout)
        self.assertFalse((self.home / ".bashrc").exists())
        result = self.run_tty([str(launcher)], [("选择:", "q\n")])
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("原生终端配置", result.stdout)
        self.log.unlink()
        second = self.run_script("install")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertFalse(any(call[0] == "systemctl" for call in self.calls()), self.calls())
        self.assertFalse(any("--doctor" in call for call in self.calls()))
        self.assertFalse((self.home / ".config").exists())

    def test_first_install_enable_only_when_requested_and_uses_both_timers(self):
        self.populated_env()
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        enables = [call for call in self.calls() if "enable" in call]
        self.assertEqual(len(enables), 1)
        self.assertIn("--now", enables[0])
        self.assertIn("trendradar-lite.timer", enables[0])
        self.assertIn("trendradar-weekly.timer", enables[0])

    def test_fresh_install_menu_save_then_install_without_network_tests(self):
        result = self.run_script("install", "--no-enable", answers=[
            ("选择:", "1\n"), ("字段编号", "1\n"), ("发件邮箱 [", "sender@example.com\n"),
            ("字段编号", "2\n"), ("邮箱密码或授权码 [", "literal-password\n"),
            ("字段编号", "3\n"), ("收件邮箱 [", "reader@example.com\n"),
            ("字段编号", "0\n"), ("选择:", "s\n"),
            ("确认规范化", "y\n"), ("确认保存并应用", "y\n"),
        ])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(read_env(self.envpath)["EMAIL_PASSWORD"], "literal-password")
        self.assertTrue((self.home / ".local/bin/trendradar").exists())
        self.assertTrue((self.units / "trendradar-lite.timer").exists())
        self.assertFalse(any("--doctor" in call or "enable" in call for call in self.calls()))

    def test_unrelated_launcher_is_never_overwritten(self):
        launcher = self.home / ".local/bin/trendradar"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\necho unrelated\n")
        result = self.run_script("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(launcher.read_text(), "#!/bin/sh\necho unrelated\n")
        self.assertFalse(self.config.exists())
        self.assertEqual(self.calls(), [])

    def test_uninstall_only_removes_owned_launcher_and_preserves_env(self):
        self.populated_env()
        result = self.run_script("install", "--no-enable")
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = self.home / ".local/bin/trendradar"
        result = self.run_script("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(launcher.exists())
        self.assertTrue(self.envpath.exists())
        launcher.write_text("#!/bin/sh\necho unrelated\n")
        result = self.run_script("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unrelated", launcher.read_text())

    def test_status_doctor_reads_literal_env_without_shell_execution(self):
        values = self.populated_env()
        sentinel = self.root / "should-not-exist"
        secret = f'$(touch "{sentinel}") `touch "{sentinel}"` $HOME \\ "quotes"'
        write_env(self.envpath, dict(values, EMAIL_PASSWORD=secret))
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())
        self.assertEqual(Path(str(self.log) + ".doctor").read_text(), secret)


if __name__ == "__main__":
    unittest.main()
