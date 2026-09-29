"""Offline regression checks for the universal wheel-only dependency lock."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lock_dependencies", ROOT / "deploy/lock_dependencies.py")
locking = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(locking)


def logical_requirements(text):
    """Join pip's continuation syntax, ignoring comments and global options."""
    logical = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        logical += " " + line.removesuffix("\\").strip()
        if line.endswith("\\"):
            continue
        if not logical.strip().startswith("--"):
            yield logical.strip()
        logical = ""
    if logical:
        raise ValueError("Unterminated requirement continuation")


class DependencyLockTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "requirements.lock").read_text()
        self.requirements = list(logical_requirements(self.text))

    def test_every_entry_is_pinned_and_has_real_shape_sha256(self):
        self.assertGreater(len(self.requirements), 40)
        for line in self.requirements:
            with self.subTest(requirement=line.split("--hash")[0]):
                self.assertRegex(line, r"^[a-zA-Z0-9_.-]+==[a-zA-Z0-9.+_-]+(?:\s|$)")
                self.assertRegex(line, r"--hash=sha256:[0-9a-f]{64}(?:\s|$)")
                self.assertNotIn("http://", line)
                self.assertNotIn("https://", line)
                for value in re.findall(r"--hash=(\S+)", line):
                    self.assertRegex(value, r"^sha256:[0-9a-f]{64}$")

    def test_input_digest_requires_regeneration_after_declaration_change(self):
        digest = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
        self.assertIn(f"# requirements.txt sha256: {digest}\n", self.text)

    def test_all_direct_requirements_are_present_in_lock(self):
        normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
        names = {normalize(line.split("==", 1)[0]) for line in self.requirements}
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                name = re.match(r"[a-zA-Z0-9_.-]+", line).group()
                self.assertIn(normalize(name), names)

    def test_lower_python_and_platform_markers_are_not_dropped(self):
        # These branches are inactive on the Linux Python 3.12 generation host.
        for name in ("async-timeout", "exceptiongroup", "tomli"):
            matches = [line for line in self.requirements if line.startswith(name + "==")]
            self.assertTrue(matches, name)
            self.assertTrue(any("python_full_version < '3.11'" in line for line in matches), name)
        self.assertTrue(any(line.startswith("colorama==") and "sys_platform == 'win32'" in line
                            for line in self.requirements))

    def test_lock_and_installers_fail_closed_without_wheels(self):
        self.assertIn("\n--only-binary :all:\n", self.text)
        for path in (ROOT / "Dockerfile", ROOT / "deploy/linux/install.sh"):
            with self.subTest(path=path):
                text = path.read_text()
                self.assertIn("--require-hashes", text)
                self.assertIn("--only-binary=:all:", text)
                self.assertIn("requirements.lock", text)
                self.assertNotRegex(text, r"pip install[^\n]*--upgrade\s+pip")
                self.assertNotRegex(text, r"pip install[^\n]*-r[^\n]*requirements\.txt")


class LockRegenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "requirements.txt").write_text("example>=1\n")
        self.destination = self.root / "requirements.lock"
        self.destination.write_text("old reviewed lock\n")
        self.compiler_output = "--only-binary :all:\nexample==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"

    def fake_compile(self, command, **kwargs):
        self.command = command
        self.environment = kwargs["env"]
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertTrue(kwargs["check"])
        candidate = Path(command[command.index("--output-file") + 1])
        self.prior_candidate = candidate.read_text() if candidate.exists() else None
        candidate.write_text(self.compiler_output)

    def test_success_uses_universal_official_index_and_isolated_temporary_cache(self):
        with patch.dict(os.environ, {"UV_INDEX": "https://untrusted.invalid", "PIP_INDEX_URL": "https://untrusted.invalid"}), \
                patch.object(locking.subprocess, "check_output", return_value=f"uv {locking.UV_VERSION}\n"), \
                patch.object(locking.subprocess, "run", side_effect=self.fake_compile):
            locking.compile_lock(self.root, "/tmp/tools/bin/uv")
        for value in ("--no-config", "--universal", "--generate-hashes", "--no-strip-markers",
                      "--no-python-downloads", "--emit-build-options"):
            self.assertIn(value, self.command)
        self.assertEqual(self.command[self.command.index("--python-version") + 1], "3.10")
        self.assertEqual(self.command[self.command.index("--only-binary") + 1], ":all:")
        self.assertEqual(self.command[self.command.index("--default-index") + 1], "https://pypi.org/simple")
        cache = Path(self.command[self.command.index("--cache-dir") + 1])
        self.assertEqual(cache.parent.parent, Path("/tmp"))
        self.assertFalse(cache.parent.exists())
        self.assertNotIn("UV_INDEX", self.environment)
        self.assertNotIn("PIP_INDEX_URL", self.environment)
        self.assertNotIn("--upgrade", self.command)
        self.assertEqual(self.prior_candidate, "old reviewed lock\n")
        self.assertIn(self.compiler_output, self.destination.read_text())

    def test_upgrade_ignores_previous_pins(self):
        with patch.object(locking.subprocess, "check_output", return_value=f"uv {locking.UV_VERSION}\n"), \
                patch.object(locking.subprocess, "run", side_effect=self.fake_compile):
            locking.compile_lock(self.root, "/tmp/tools/bin/uv", upgrade=True)
        self.assertIn("--upgrade", self.command)
        self.assertIsNone(self.prior_candidate)

    def test_failed_resolution_preserves_existing_lock(self):
        with patch.object(locking.subprocess, "check_output", return_value=f"uv {locking.UV_VERSION}\n"), \
                patch.object(locking.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "uv")):
            with self.assertRaises(subprocess.CalledProcessError):
                locking.compile_lock(self.root, "/tmp/tools/bin/uv")
        self.assertEqual(self.destination.read_text(), "old reviewed lock\n")

    def test_wrong_tool_version_preserves_existing_lock(self):
        with patch.object(locking.subprocess, "check_output", return_value="uv 0.0.0\n"), \
                patch.object(locking.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                locking.compile_lock(self.root, "/tmp/tools/bin/uv")
            run.assert_not_called()
        self.assertEqual(self.destination.read_text(), "old reviewed lock\n")

    def test_unhashed_or_source_enabled_output_preserves_existing_lock(self):
        for invalid in ("example==1.0\n", "example==1.0 --hash=sha256:" + "a" * 64 + "\n"):
            with self.subTest(output=invalid), \
                    patch.object(locking.subprocess, "check_output", return_value=f"uv {locking.UV_VERSION}\n"), \
                    patch.object(locking.subprocess, "run", side_effect=self.fake_compile):
                self.compiler_output = invalid
                with self.assertRaises(ValueError):
                    locking.compile_lock(self.root, "/tmp/tools/bin/uv")
            self.assertEqual(self.destination.read_text(), "old reviewed lock\n")


if __name__ == "__main__":
    unittest.main()
