# coding=utf-8
"""AI 配置与 source_cap_ratio 归一化回归测试。"""

import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trendradar.ai.selector import SOURCE_CAP_RATIO, _normalize_source_cap_ratio
from trendradar.core import loader
from trendradar.core.loader import _load_ai_analysis_config, _load_ai_config


class SourceCapRatioConfigTests(unittest.TestCase):
    def test_missing_key_uses_default(self):
        config = _load_ai_analysis_config({"ai_analysis": {}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], 0.30)

    def test_empty_yaml_value_uses_default(self):
        config = _load_ai_analysis_config({"ai_analysis": {"source_cap_ratio": None}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], 0.30)

    def test_explicit_zero_is_preserved_by_loader(self):
        config = _load_ai_analysis_config({"ai_analysis": {"source_cap_ratio": 0}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], 0)

    def test_numeric_string_is_preserved_by_loader(self):
        config = _load_ai_analysis_config({"ai_analysis": {"source_cap_ratio": "0.2"}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], "0.2")

    def test_api_key_file_is_used_when_direct_key_is_unset(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as handle:
            handle.write("file-secret\n")
            secret_path = handle.name
        try:
            with patch.dict(os.environ, {"AI_API_KEY_FILE": secret_path}, clear=True):
                self.assertEqual(_load_ai_config({"ai": {}})["API_KEY"], "file-secret")
        finally:
            os.unlink(secret_path)


class SourceCapRatioNormalizationTests(unittest.TestCase):
    def test_none_uses_default(self):
        self.assertEqual(_normalize_source_cap_ratio(None), SOURCE_CAP_RATIO)

    def test_empty_string_uses_default(self):
        self.assertEqual(_normalize_source_cap_ratio(""), SOURCE_CAP_RATIO)

    def test_non_numeric_uses_default(self):
        self.assertEqual(_normalize_source_cap_ratio("abc"), SOURCE_CAP_RATIO)

    def test_zero_is_raised_to_minimum(self):
        self.assertEqual(_normalize_source_cap_ratio(0), 0.01)

    def test_above_one_is_clamped(self):
        self.assertEqual(_normalize_source_cap_ratio(2.5), 1.0)

    def test_valid_ratio_is_kept(self):
        self.assertEqual(_normalize_source_cap_ratio(0.2), 0.2)


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "POSIX private-file reader")
class SecretFileSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = self.root / "synthetic-private-key"
        self.secret.write_text("file-secret\n", encoding="utf-8")
        self.secret.chmod(0o600)

    def reject(self, path):
        with self.assertRaises(ValueError) as raised:
            loader._read_secret_file(str(path))
        self.assertNotIn(str(path), str(raised.exception))
        self.assertNotIn("file-secret", str(raised.exception))

    def test_owner_only_file_and_sticky_tmp_are_supported(self):
        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                self.secret.chmod(mode)
                self.assertEqual(loader._read_secret_file(str(self.secret)), "file-secret")

    def test_relative_file_is_read_from_current_directory(self):
        with patch.object(loader.Path, "cwd", return_value=self.root):
            self.assertEqual(loader._read_secret_file(self.secret.name), "file-secret")

    def test_group_or_other_permissions_and_execute_bits_are_rejected(self):
        for mode in (0o640, 0o604, 0o660, 0o606, 0o644, 0o700, 0o4600):
            with self.subTest(mode=oct(mode)):
                self.secret.chmod(mode)
                self.reject(self.secret)

    def test_final_symlink_is_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.secret)
        self.reject(link)

    def test_ancestor_symlink_is_rejected_even_above_direct_parent(self):
        real = self.root / "real"
        (real / "nested").mkdir(parents=True, mode=0o700)
        target = real / "nested" / "key"
        target.write_text("file-secret", encoding="utf-8")
        target.chmod(0o600)
        link = self.root / "linked"
        link.symlink_to(real, target_is_directory=True)
        self.reject(link / "nested" / "key")
        # A lexical '..' must not normalize away the symlink before validation.
        self.reject(link / ".." / self.secret.name)

    def test_writable_ancestor_is_rejected(self):
        unsafe = self.root / "unsafe"
        (unsafe / "private").mkdir(parents=True, mode=0o700)
        target = unsafe / "private" / "key"
        target.write_text("file-secret", encoding="utf-8")
        target.chmod(0o600)
        for mode in (0o770, 0o707):
            with self.subTest(mode=oct(mode)):
                unsafe.chmod(mode)
                self.reject(target)

    def test_directory_missing_and_invalid_utf8_fail_closed(self):
        self.reject(self.root)
        self.reject(self.root / "missing-synthetic-private-key")
        self.secret.write_bytes(b"\xfffile-secret")
        self.reject(self.secret)

    def test_permission_error_does_not_fall_back_to_yaml(self):
        with patch.object(loader.os, "open", side_effect=PermissionError("private-path")):
            with patch.dict(os.environ, {"AI_API_KEY_FILE": str(self.secret)}, clear=True):
                with self.assertRaisesRegex(ValueError, "无法安全读取"):
                    _load_ai_config({"ai": {"api_key": "yaml-key"}})

    def test_size_limit_checks_metadata_and_actual_read(self):
        self.secret.write_bytes(b"12345")
        with patch.object(loader, "MAX_SECRET_FILE_BYTES", 4):
            self.reject(self.secret)
        # Grow the same inode after the metadata snapshot; bounded read still rejects.
        real_fstat = os.fstat
        def grow(descriptor):
            result = real_fstat(descriptor)
            if result.st_ino == self.secret.stat().st_ino:
                with self.secret.open("ab") as stream:
                    stream.write(b"6789")
            return result
        with patch.object(loader, "MAX_SECRET_FILE_BYTES", 8):
            with patch.object(loader.os, "fstat", side_effect=grow):
                self.reject(self.secret)

    def test_direct_key_has_precedence_without_opening_file(self):
        with patch.object(loader, "_read_secret_file") as read:
            with patch.dict(os.environ, {"AI_API_KEY": "direct-key", "AI_API_KEY_FILE": "/missing"}, clear=True):
                self.assertEqual(_load_ai_config({"ai": {}})["API_KEY"], "direct-key")
        read.assert_not_called()

    def test_atomic_file_replacement_keeps_original_descriptor_snapshot(self):
        replacement = self.root / "replacement"
        replacement.write_text("replacement-key", encoding="utf-8")
        replacement.chmod(0o600)
        real_open = os.open
        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == self.secret.name:
                replacement.replace(self.secret)
            return descriptor
        with patch.object(loader.os, "open", side_effect=replace_after_open):
            self.assertEqual(loader._read_secret_file(str(self.secret)), "file-secret")
        self.assertEqual(loader._read_secret_file(str(self.secret)), "replacement-key")

    def test_ancestor_replacement_cannot_redirect_pinned_directory(self):
        directory = self.root / "original"
        directory.mkdir(mode=0o700)
        target = directory / "key"
        target.write_text("file-secret", encoding="utf-8")
        target.chmod(0o600)
        other = self.root / "attacker"
        other.mkdir(mode=0o700)
        (other / "key").write_text("wrong-key", encoding="utf-8")
        (other / "key").chmod(0o600)
        real_open = os.open
        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == "original":
                directory.rename(self.root / "moved")
                directory.symlink_to(other, target_is_directory=True)
            return descriptor
        with patch.object(loader.os, "open", side_effect=replace_after_open):
            self.assertEqual(loader._read_secret_file(str(target)), "file-secret")

    def test_fifo_is_rejected_without_waiting_for_writer(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        def timed_out(signum, frame):
            raise TimeoutError("private-file reader blocked on FIFO")
        old_handler = signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(3)
        try:
            self.reject(fifo)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    @unittest.skipUnless(Path("/proc/self/fd").exists(), "Linux descriptor inventory")
    def test_failed_reads_close_all_descriptors(self):
        count = len(list(Path("/proc/self/fd").iterdir()))
        self.secret.chmod(0o644)
        for _ in range(20):
            self.reject(self.secret)
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), count)


if __name__ == "__main__":
    unittest.main()
