import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.docker import runtime_config as runtime
from deploy.envfile import EnvDocument, atomic_write


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "env"
        self.base = {
            runtime.RUNTIME_ENV_KEY: str(self.path), "PATH": "/controlled/bin", "PYTHONPATH": "/controlled/lib",
            "DOCKER_CONTAINER": "false", "STORAGE_BACKEND": "remote", "TZ": "Etc/GMT+5",
            "AI_API_KEY": "old-key", "AI_API_KEY_FILE": "/old-secret", "EMAIL_PASSWORD": "old-mail",
            "EMAIL_TO": "old@example.invalid", "S3_SECRET_ACCESS_KEY": "old-s3",
            "SCHEDULE_ENABLED": "false", "MORNING_PUSH_TIME": "04:32", "CRAWLER_MINUTE": "42",
            "CONFIG_PATH": "/old-config", "FREQUENCY_WORDS_PATH": "/old-keywords",
            "LOCAL_RETENTION_DAYS": "91", "OPENAI_API_KEY": "provider-old",
            "TRENDRADAR_FORCE_RUN": "1", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }

    def put(self, text="TZ=Asia/Shanghai\n", mode=0o600):
        atomic_write(self.path, text.encode(), mode)

    def assert_error(self, code, call):
        with self.assertRaises(runtime.RuntimeConfigError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), runtime.ERRORS[code])
        self.assertNotIn(str(self.path), str(raised.exception))

    def test_add_modify_delete_rebuilds_environment_and_never_mutates_global(self):
        before = dict(os.environ)
        self.put("AI_API_KEY=first\nEMAIL_PASSWORD=first-mail\nS3_SECRET_ACCESS_KEY=first-s3\nCRAWLER_MINUTE=7\n")
        first = runtime.load_runtime_config(self.base)
        self.put("AI_API_KEY=second\nEMAIL_TO=new@example.invalid\nTZ=UTC\n")
        second = runtime.load_runtime_config(self.base)
        self.assertEqual(first.env["AI_API_KEY"], "first")
        self.assertEqual(second.env["AI_API_KEY"], "second")
        self.assertEqual(second.env["EMAIL_TO"], "new@example.invalid")
        self.put("TZ=UTC\n")
        third = runtime.load_runtime_config(self.base)
        for key in self.base:
            if key not in runtime.BASE_ENV_KEYS and key not in (runtime.RUNTIME_ENV_KEY, "TZ", "DOCKER_CONTAINER", "STORAGE_BACKEND"):
                self.assertNotIn(key, third.env)
        self.assertEqual(third.env["PATH"], "/controlled/bin")
        self.assertEqual(third.env["PYTHONPATH"], "/controlled/lib")
        self.assertEqual(third.env["DOCKER_CONTAINER"], "true")
        self.assertEqual(third.env["STORAGE_BACKEND"], "local")
        self.assertEqual(third.settings.crawler_minute, 0)
        self.assertEqual(third.settings.push_times, frozenset(runtime.DEFAULT_TIMES))
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.base["AI_API_KEY"], "old-key")
        self.assertNotIn("first", repr(first))
        with self.assertRaises(TypeError):
            first.env["AI_API_KEY"] = "no mutation"

    def test_unset_control_variable_is_legacy_only_empty_is_not(self):
        base = {"AI_API_KEY": "legacy", "TZ": "UTC", "CRAWLER_MINUTE": "8"}
        snapshot = runtime.load_runtime_config(base)
        self.assertFalse(snapshot.external)
        self.assertEqual(snapshot.env["AI_API_KEY"], "legacy")
        self.assertEqual(snapshot.settings.crawler_minute, 8)
        self.assert_error("path", lambda: runtime.load_runtime_config({runtime.RUNTIME_ENV_KEY: ""}))

    def test_missing_empty_broken_and_deleted_file_never_fall_back(self):
        self.assert_error("missing", lambda: runtime.load_runtime_config(self.base))
        for content, code in (("", "empty"), ("# comment\n", "empty"), ("AI_API_KEY='SECRET\n", "format"),
                              ("export AI_API_KEY=SECRET\n", "format"), ("TZ=SECRET_BAD_ZONE\n", "schedule")):
            self.put(content)
            self.assert_error(code, lambda: runtime.load_runtime_config(self.base))
        self.put()
        runtime.load_runtime_config(self.base)
        self.path.unlink()
        self.assert_error("missing", lambda: runtime.load_runtime_config(self.base))
        atomic_write(self.path, b"AI_API_KEY=\xff\n")
        self.assert_error("format", lambda: runtime.read_runtime_env(self.path))

    def test_permissions_regular_file_and_no_symlink_parents(self):
        for mode in (0o644, 0o640, 0o660, 0o604, 0o700, 0o1600, 0o4600):
            self.put(mode=mode)
            self.path.chmod(mode)  # A write can clear setuid bits on Linux.
            self.assert_error("permissions", lambda: runtime.read_runtime_env(self.path))
        for mode in (0o400, 0o600):
            self.put(mode=mode)
            self.assertEqual(runtime.read_runtime_env(self.path)["TZ"], "Asia/Shanghai")
        self.path.unlink()
        self.path.mkdir()
        self.assert_error("not_regular", lambda: runtime.read_runtime_env(self.path))
        self.path.rmdir()
        target = self.root / "secret"
        atomic_write(target, b"TZ=UTC\n")
        self.path.symlink_to(target)
        self.assert_error("symlink", lambda: runtime.read_runtime_env(self.path))
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.assert_error("not_regular", lambda: runtime.read_runtime_env(self.path))
        link = self.root / "linked-dir"
        link.symlink_to(self.root, target_is_directory=True)
        self.assert_error("symlink", lambda: runtime.read_runtime_env(link / "secret"))

    def test_inaccessible_and_size_errors_are_fixed(self):
        self.put()
        with patch.object(runtime.os, "open", side_effect=PermissionError("SECRET path")):
            self.assert_error("unreadable", lambda: runtime.read_runtime_env(self.path))
        with patch.object(runtime, "MAX_ENV_BYTES", 2):
            self.assert_error("too_large", lambda: runtime.read_runtime_env(self.path))

    def test_exact_byte_limit_is_shared_by_parser_and_file_reader(self):
        header = "TZ=UTC\n# 中文 ".encode()
        content = header + b"x" * (runtime.MAX_ENV_BYTES - len(header) - 1) + b"\n"
        self.assertEqual(len(content), runtime.MAX_ENV_BYTES)
        atomic_write(self.path, content)
        self.assertEqual(runtime.parse_runtime_env(content), {"TZ": "UTC"})
        self.assertEqual(runtime.read_runtime_env(self.path), {"TZ": "UTC"})
        oversized = content + b"#"
        self.assert_error("too_large", lambda: runtime.parse_runtime_env(oversized))
        atomic_write(self.path, oversized)
        self.assert_error("too_large", lambda: runtime.read_runtime_env(self.path))

    def test_save_validation_rejects_oversized_final_render_before_writing(self):
        from deploy.docker.docker_configure import DockerApplication
        settings = (b"TZ=UTC\nEMAIL_FROM=sender@example.invalid\nEMAIL_TO=reader@example.invalid\n"
                    b"EMAIL_PASSWORD=synthetic-only\nAI_ANALYSIS_ENABLED=false\n")
        content = settings + b"#" + b"x" * (runtime.MAX_ENV_BYTES - len(settings) - 2) + b"\n"
        atomic_write(self.path, content)
        application = DockerApplication(self.path)
        application.save(dict(application.values))
        self.assertEqual(self.path.stat().st_size, runtime.MAX_ENV_BYTES)
        self.assertEqual(runtime.read_runtime_env(self.path)["TZ"], "UTC")
        before = self.path.read_bytes()
        application = DockerApplication(self.path)
        with self.assertRaisesRegex(Exception, "runtime configuration file is too large"):
            application.save(dict(application.values, EMAIL_TO="longer-reader@example.invalid"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replacement_sees_next_file_and_current_read_has_one_inode(self):
        self.put("AI_API_KEY=first\n")
        first_ino = self.path.stat().st_ino
        original_fstat = runtime.os.fstat
        replaced = False

        def replacing_fstat(fd):
            nonlocal replaced
            info = original_fstat(fd)
            if not replaced:
                replaced = True
                self.put("# TrendRadar env format: docker\nAI_API_KEY=\"second$$key\"\n")
            return info

        with patch.object(runtime.os, "fstat", side_effect=replacing_fstat):
            first = runtime.read_runtime_env(self.path)
        second = runtime.read_runtime_env(self.path)
        self.assertNotEqual(first_ino, self.path.stat().st_ino)
        self.assertEqual(first["AI_API_KEY"], "first")
        self.assertEqual(second["AI_API_KEY"], "second$key")

    def test_literal_and_legacy_formats_round_trip_shared_env_document(self):
        value = '密钥 $one $$two ${HOME} $(touch /tmp/never) `id` # ; \\ " single\''
        for syntax in ("linux", "docker"):
            if self.path.exists():
                self.path.unlink()
            document = EnvDocument(self.path, syntax)
            document.save({"AI_API_KEY": value, "EMAIL_PASSWORD": value})
            content = self.path.read_bytes()
            self.assertEqual(runtime.detect_format(content), syntax)
            self.assertEqual(runtime.read_runtime_env(self.path)["AI_API_KEY"], value)
            self.assertEqual(runtime.read_runtime_env(self.path)["EMAIL_PASSWORD"], value)
        literal = b'AI_API_KEY="$$UNCHANGED ${UNCHANGED}"\n'
        self.assertEqual(runtime.parse_runtime_env(literal)["AI_API_KEY"], "$$UNCHANGED ${UNCHANGED}")

    def test_unsupported_execution_control_keys_and_locked_identity(self):
        for key in ("PATH", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "HOME",
                    runtime.RUNTIME_ENV_KEY, "TRENDRADAR_FORCE_RUN", "UNKNOWN_KEY"):
            self.assert_error("unsupported", lambda: runtime.parse_runtime_env(f"{key}=SECRET\n".encode()))
        for line in ("DOCKER_CONTAINER=false", "STORAGE_BACKEND=remote"):
            self.assert_error("locked", lambda: runtime.parse_runtime_env((line + "\n").encode()))
        runtime.parse_runtime_env(b"DOCKER_CONTAINER=true\nSTORAGE_BACKEND=local\n")

    def test_all_existing_application_loader_keys_are_supported(self):
        source = (Path(__file__).resolve().parents[1] / "trendradar/core/loader.py").read_text()
        keys = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.startswith("_get_env_"):
                keys.update(arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
        self.assertTrue(keys)
        self.assertEqual(sorted(key for key in keys if not runtime.is_application_key(key)), [])

    def test_schedule_validation_and_timezone_only(self):
        for line in ("CRAWLER_MINUTE=60", "WEEKLY_WEEKDAY=7", "WEEKLY_HOUR=24", "WEEKLY_MINUTE=-1",
                     "MORNING_PUSH_TIME=7:00", "EVENING_PUSH_TIME=25:00", "SCHEDULER_POLL_SECONDS=bad",
                     "SCHEDULER_MAX_ATTEMPTS=0", "TZ=UTC\nTIMEZONE=Asia/Shanghai"):
            self.assert_error("schedule", lambda: runtime.parse_runtime_env((line + "\n").encode()))
        self.put("TIMEZONE=UTC\n")
        snapshot = runtime.load_runtime_config(self.base)
        self.assertEqual(snapshot.env["TZ"], "UTC")
        self.assertEqual(snapshot.settings.timezone.key, "UTC")

    def test_cleared_optional_schedule_fields_use_defaults(self):
        values = runtime.parse_runtime_env(
            b"CRAWLER_MINUTE=\nMORNING_PUSH_TIME=\nNOON_PUSH_TIME=\nEVENING_PUSH_TIME=\n"
            b"DAILY_SUMMARY_TIME=\nWEEKLY_WEEKDAY=\nWEEKLY_HOUR=\nWEEKLY_MINUTE=\n"
            b"SCHEDULER_POLL_SECONDS=\nSCHEDULER_MAX_ATTEMPTS=\nTZ=UTC\n"
        )
        settings = runtime.schedule_settings(values)
        self.assertEqual(settings.crawler_minute, 0)
        self.assertEqual(settings.push_times, frozenset(runtime.DEFAULT_TIMES))
        self.assertEqual((settings.weekly_weekday, settings.weekly_hour, settings.weekly_minute), (6, 12, 30))
        self.assertEqual((settings.poll_seconds, settings.max_attempts), (20, 3))


if __name__ == "__main__":
    unittest.main()
