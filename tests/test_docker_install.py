"""Host lifecycle tests use a fake Docker executable and private temporary clones."""
import contextlib
import io
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/docker"))
import manage
from envfile import ConfigError, read_env, write_env

FAKE_DOCKER = '''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["FAKE_ROOT"])
with open(os.environ["FAKE_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] == ["image", "inspect"]:
    print(os.environ.get("FAKE_IMAGE_COMPATIBILITY", "1"))
    sys.exit(int(os.environ.get("FAKE_IMAGE_MISSING", "0")))
if "config" in args and "--images" in args:
    print("test/trendradar:local")
    sys.exit(0)
if "run" in args:
    if "--pull" not in args or args[args.index("--pull") + 1] != "never" or "--no-deps" not in args:
        sys.exit(89)
    if "volume-init" in args:
        if "setup" in args[args.index("run"):] or "--service-ports" in args:
            sys.exit(88)
        sys.exit(0)
    if "--entrypoint" in args:
        command = args[args.index("deploy/docker/manage.py") + 1]
        if command == "init-volume":
            # The project/config setup container must never initialize output.
            sys.exit(88)
        sys.exit(subprocess.call([sys.executable, str(root / "deploy/docker/manage.py"), command, "--root", str(root)]))
    forced = os.environ.get("FAKE_SETUP_EXIT")
    if forced is not None:
        sys.exit(int(forced))
    mode = args[args.index("--mode") + 1]
    if mode == "web":
        # Browser behavior is covered in-memory by test_docker_menu; never bind.
        sys.exit(2)
    sys.exit(subprocess.call([sys.executable, str(root / "deploy/configure.py"), "--deployment", "docker", "--runtime-config", "--output", str(root / "runtime/env"), "--mode", "terminal"]))
sys.exit(0)
'''

VALID = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com", "EMAIL_PASSWORD": "private-$secret", "TZ": "UTC"}


class DockerInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "clone with spaces"
        self.root.mkdir()
        shutil.copytree(ROOT / "deploy", self.root / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / ".env.example", self.root / ".env.example")
        shutil.copy2(ROOT / "compose.yaml", self.root / "compose.yaml")
        self.home = base / "home"
        self.home.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text(FAKE_DOCKER.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        fake.chmod(0o755)
        self.log = base / "docker.log"
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}",
                        FAKE_ROOT=str(self.root), FAKE_LOG=str(self.log), LITELLM_LOCAL_MODEL_COST_MAP="True")
        self.env.pop("TRENDRADAR_UID", None)
        self.env.pop("TRENDRADAR_GID", None)
        self.path = self.root / "runtime/env"

    def run_script(self, *args, input="", script="install.sh", **env):
        return subprocess.run(["bash", str(self.root / "deploy/docker" / script), *args], input=input,
                              text=True, capture_output=True, env=dict(self.env, **env), cwd=self.home, timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_no_upgrade(self):
        for call in self.calls():
            self.assertNotIn("pull", call)
            self.assertNotIn("build", call)
            self.assertNotIn("--build", call)
            self.assertNotIn("up", call)
            self.assertNotIn("init-volume", call)
            self.assertNotIn("volume-init", call)

    def test_new_install_cancel_and_eof_never_create_runtime_or_start(self):
        result = self.run_script("--no-start", "--terminal", input="q\ny\n")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.path.exists())
        self.assertNotIn("up", [word for call in self.calls() for word in call])
        self.assertNotIn("init-volume", [word for call in self.calls() for word in call])
        self.assertNotIn("volume-init", [word for call in self.calls() for word in call])
        self.assertEqual(sum("pull" in call for call in self.calls()), 1)
        self.assertFalse(any("build" in call for call in self.calls()))
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / ".env.backups").exists())
        result = self.run_script("--configure", "--terminal", input="")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.path.exists())

    def test_new_install_save_no_start_and_any_directory_launcher(self):
        answers = "1\n1\nsender@example.com\n2\nprivate-dollar$secret\n3\nreader@example.com\n0\ns\ny\n"
        result = self.run_script("--no-start", "--terminal", input=answers)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], "private-dollar$secret")
        self.assertNotIn("private-dollar$secret", result.stdout + result.stderr)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("up", [word for call in self.calls() for word in call])
        launcher = self.home / ".local/bin/trendradar-docker"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertFalse((self.home / ".bashrc").exists())
        self.log.write_text("")
        launched = subprocess.run([str(launcher), "--terminal"], input="q\n", text=True,
                                  capture_output=True, env=self.env, cwd=self.home, timeout=15)
        self.assertEqual(launched.returncode, 2)
        self.assert_no_upgrade()

    def test_existing_install_keeps_runtime_and_does_not_implicitly_update_image(self):
        write_env(self.path, VALID)
        before = self.path.read_bytes()
        result = self.run_script("--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_no_upgrade()
        self.assertFalse(any("--mode" in call for call in self.calls()))

    def test_configure_saves_without_pull_build_up_or_output_initialization(self):
        write_env(self.path, VALID)
        result = self.run_script("--configure", "--terminal", input="2\n2\nopenai/new\n0\ns\ny\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_env(self.path)["AI_MODEL"], "openai/new")
        self.assert_no_upgrade()
        self.assertFalse(any("prepare" in call or "check" in call for call in self.calls()))

    def test_configure_legacy_cancel_never_writes_runtime_or_private_backup(self):
        legacy = self.root / ".env"
        write_env(legacy, VALID, "docker")
        before = legacy.read_bytes()
        result = self.run_script("--configure", "--terminal", input="q\ny\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".env.backups").exists())
        self.assertEqual(legacy.read_bytes(), before)
        self.assert_no_upgrade()

    def test_configure_legacy_save_migrates_without_retyping_and_no_start(self):
        legacy = self.root / ".env"
        write_env(legacy, VALID, "docker")
        result = self.run_script("--configure", "--terminal", input="s\ny\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], VALID["EMAIL_PASSWORD"])
        self.assertTrue((self.root / ".env.backups").is_dir())
        self.assert_no_upgrade()

    def test_missing_local_image_and_setup_failure_have_no_fallback(self):
        write_env(self.path, VALID)
        result = self.run_script("--configure", "--terminal", FAKE_IMAGE_MISSING="1")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(any("run" in call for call in self.calls()))
        self.assert_no_upgrade()
        self.log.write_text("")
        result = self.run_script("--configure", "--terminal", FAKE_SETUP_EXIT="17")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(sum("run" in call for call in self.calls()), 1)
        self.assert_no_upgrade()

    def test_incompatible_image_is_refused_without_setup_or_automatic_build(self):
        write_env(self.path, VALID)
        result = self.run_script("--configure", "--terminal", FAKE_IMAGE_COMPATIBILITY="<no value>")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not support runtime configuration", result.stderr)
        self.assertFalse(any("run" in call for call in self.calls()))
        self.assert_no_upgrade()

    def test_setup_success_without_saved_file_does_not_start(self):
        result = self.run_script("--terminal", FAKE_SETUP_EXIT="0")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(any(set(call) & {"up", "init-volume", "volume-init", "persist-identity"} for call in self.calls()))

    def test_install_migrates_legacy_only_after_confirmation_and_preserves_private_backup(self):
        legacy = self.root / ".env"
        write_env(legacy, dict(VALID, TREND_RADAR_IMAGE="fixed-image", SETUP_PORT="9999"), "docker")
        before = legacy.read_bytes()
        result = self.run_script("--no-start", "--terminal", input="s\ny\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], VALID["EMAIL_PASSWORD"])
        self.assertNotIn("TREND_RADAR_IMAGE", read_env(self.path))
        self.assertNotIn(VALID["EMAIL_PASSWORD"], result.stdout + result.stderr)
        backups = list((self.root / ".env.backups").iterdir())
        self.assertTrue(any(item.read_bytes() == before for item in backups))
        self.assertTrue(all(item.stat().st_mode & 0o777 == 0o600 for item in backups))
        self.assertTrue(any("--mode" in call for call in self.calls()))

    def test_legacy_install_cancel_preserves_original_and_does_not_start(self):
        legacy = self.root / ".env"
        write_env(legacy, VALID, "docker")
        before = legacy.read_bytes()
        result = self.run_script("--terminal", input="q\ny\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".env.backups").exists())
        self.assertFalse(any(set(call) & {"up", "init-volume", "volume-init", "persist-identity"} for call in self.calls()))

    def test_copied_template_can_cancel_then_fill_mail_and_install(self):
        legacy = self.root / ".env"
        legacy.write_bytes((ROOT / ".env.example").read_bytes())
        before = legacy.read_bytes()
        result = self.run_script("--terminal", input="q\ny\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.path.exists())
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse((self.root / ".env.backups").exists())
        self.assertTrue(any("--mode" in call for call in self.calls()))
        answers = "1\n1\nsender@example.com\n2\nnew-secret\n3\nreader@example.com\n0\ns\ny\n"
        result = self.run_script("--terminal", input=answers)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], "new-secret")
        self.assertTrue(any("up" in call for call in self.calls()))

    def test_existing_invalid_runtime_enters_menu_and_can_be_repaired(self):
        write_env(self.path, {"TZ": "UTC"})
        answers = "1\n1\nsender@example.com\n2\nnew-secret\n3\nreader@example.com\n0\ns\ny\n"
        result = self.run_script("--no-start", "--terminal", input=answers)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], "new-secret")

    def test_update_legacy_cancel_then_confirm_installs_launcher(self):
        legacy = self.root / ".env"
        write_env(legacy, VALID, "docker")
        before = legacy.read_bytes()
        result = self.run_script("--build", "--terminal", input="q\ny\n", script="update.sh")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(any("--mode" in call for call in self.calls()))
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".env.backups").exists())
        self.assertFalse(any(set(call) & {"up", "init-volume", "volume-init", "persist-identity"} for call in self.calls()))
        result = self.run_script("--terminal", "--pull", input="s\ny\n", script="update.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], VALID["EMAIL_PASSWORD"])
        self.assertTrue(os.access(self.home / ".local/bin/trendradar-docker", os.X_OK))

    def test_update_refuses_foreign_launcher_before_image_or_config_changes(self):
        launcher = self.home / ".local/bin/trendradar-docker"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("unrelated executable")
        write_env(self.root / ".env", VALID, "docker")
        result = self.run_script(script="update.sh")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(launcher.read_text(), "unrelated executable")
        self.assertFalse(any("pull" in call or "build" in call or "run" in call or "up" in call for call in self.calls()))

    def test_update_web_cancel_does_not_persist_identity_or_start(self):
        write_env(self.root / ".env", VALID, "docker")
        before = (self.root / ".env").read_bytes()
        result = self.run_script("--web", script="update.sh")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        setup_call = next(call for call in self.calls() if "--mode" in call)
        self.assertEqual(setup_call[-1], "web")
        self.assertIn("--service-ports", setup_call)
        self.assertIn("-T", setup_call)
        self.assertEqual((self.root / ".env").read_bytes(), before)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".env.backups").exists())
        self.assertFalse(any(set(call) & {"up", "volume-init", "persist-identity"} for call in self.calls()))

    def test_stranger_launcher_refused_before_build_and_symlink_refused(self):
        launcher = self.home / ".local/bin/trendradar-docker"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("unrelated executable")
        result = self.run_script("--no-start")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(launcher.read_text(), "unrelated executable")
        self.assertFalse(any("build" in call or "run" in call for call in self.calls()))
        launcher.unlink()
        launcher.symlink_to(self.root / ".env.example")
        result = self.run_script("--no-start")
        self.assertEqual(result.returncode, 2)

    def test_update_is_explicit_and_only_then_initializes_and_recreates(self):
        write_env(self.path, VALID)
        before = self.path.read_bytes()
        result = self.run_script("--build", script="update.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        calls = self.calls()
        self.assertTrue(any("build" in call for call in calls))
        self.assertTrue(any("volume-init" in call for call in calls))
        up = next(call for call in calls if "up" in call)
        self.assertIn("--no-build", up)
        self.assertEqual(up[up.index("--pull") + 1], "never")
        self.assertFalse(any("--mode" in call for call in calls))
        self.assertTrue(os.access(self.home / ".local/bin/trendradar-docker", os.X_OK))

    def test_start_only_after_check_and_volume_initialization(self):
        write_env(self.path, VALID)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        check = next(i for i, call in enumerate(calls) if "check" in call)
        init = next(i for i, call in enumerate(calls) if "volume-init" in call)
        up = next(i for i, call in enumerate(calls) if "up" in call)
        self.assertLess(check, init)
        self.assertLess(init, up)
        persist = next(i for i, call in enumerate(calls) if "persist-identity" in call)
        self.assertLess(check, persist)
        self.assertLess(persist, init)
        self.assertNotIn("setup", calls[init][calls[init].index("run"):])
        self.assertEqual(calls[init][-1], "volume-init")
        self.assertIn("TRENDRADAR_UID", calls[init])
        self.assertIn("TRENDRADAR_GID", calls[init])

    def test_tty_cancel_is_final(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.home)
            os.execve("/bin/bash", ["bash", str(self.root / "deploy/docker/install.sh"), "--no-start"], self.env)
        transcript = b""
        sent = False
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(fd, 8192)
                    except OSError:
                        break
                    if not data:
                        break
                    transcript += data
                    if not sent and "选择:".encode() in transcript:
                        os.write(fd, b"q\ny\n")
                        sent = True
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail("fake-Docker TTY setup timed out")
        finally:
            os.close(fd)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 2, transcript.decode(errors="replace"))
        self.assertFalse(self.path.exists())
        self.assertFalse(any("up" in call for call in self.calls()))

    def test_uninstall_stop_preserves_and_purge_removes_private_state(self):
        write_env(self.path, VALID)
        self.run_script("--no-start")
        result = self.run_script(script="uninstall.sh")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.path.exists())
        result = self.run_script("--purge-data", script="uninstall.sh")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.home / ".local/bin/trendradar-docker").exists())


class DockerOwnershipTests(unittest.TestCase):
    def test_identity_command_requires_saved_valid_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for values in (None, {"TZ": "UTC"}):
                if values is not None:
                    write_env(root / "runtime/env", values)
                with contextlib.redirect_stderr(io.StringIO()):
                    code = manage.main(["persist-identity", "--root", str(root)])
                self.assertEqual(code, 2)
                self.assertFalse((root / ".env").exists())
                self.assertFalse((root / ".env.backups").exists())

    def test_prepare_is_read_only_even_with_legacy_application_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_env(root / ".env", VALID, "docker")
            before = (root / ".env").read_bytes()
            with mock.patch.object(manage.os, "chown") as chown, mock.patch.object(manage.os, "chmod") as chmod:
                manage.prepare(root, (2345, 3456))
            self.assertFalse((root / "runtime/env").exists())
            self.assertFalse((root / ".env.backups").exists())
            self.assertEqual((root / ".env").read_bytes(), before)
            chown.assert_not_called()
            chmod.assert_not_called()

    def test_non_root_ids_and_reject_root(self):
        with mock.patch.dict(os.environ, {"TRENDRADAR_UID": "2345", "TRENDRADAR_GID": "3456"}):
            self.assertEqual(manage.target_identity(), (2345, 3456))
        for uid in ("0", "-1", "not-id", "99999999999"):
            with mock.patch.dict(os.environ, {"TRENDRADAR_UID": uid, "TRENDRADAR_GID": "1000"}):
                with self.assertRaises(ConfigError):
                    manage.target_identity()

    def test_init_volume_does_not_follow_symlinks_and_has_no_cli_path_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / "data").write_text("payload")
            with mock.patch.object(manage.os, "chown") as chown, contextlib.redirect_stdout(io.StringIO()):
                manage.init_volume((2345, 3456), output=output)
            self.assertEqual(chown.call_count, 2)
            self.assertTrue(all(call.kwargs == {"follow_symlinks": False} for call in chown.call_args_list))
            (output / "link").symlink_to(Path(tmp) / "external")
            with mock.patch.object(manage.os, "chown") as chown:
                with self.assertRaises(ConfigError):
                    manage.init_volume((2345, 3456), output=output)
                chown.assert_not_called()

    def test_root_setup_corrects_runtime_and_backup_owner_without_mode_644(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime/env"
            write_env(path, VALID)
            write_env(path, dict(VALID, EMAIL_TO="next@example.com"))
            with mock.patch.object(manage.os, "geteuid", return_value=0), mock.patch.object(manage.os, "chown") as chown:
                manage.private_runtime(path, (2345, 3456))
            self.assertGreaterEqual(chown.call_count, 4)
            self.assertTrue(all(call.args[1:] == (2345, 3456) for call in chown.call_args_list))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
