"""Offline base-image pinning contracts; registry validity needs live verification."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerBaseImageTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text()
        self.instructions = re.sub(r"\\\n", " ", self.dockerfile).splitlines()

    def test_base_is_literal_patch_distro_and_digest_not_build_arg_or_platform_pin(self):
        # A version tag alone remains mutable. The literal index allows Docker
        # to choose either platform; no build argument may silently unpin it.
        instructions = [line.strip() for line in self.instructions
                        if re.match(r"\s*FROM\s", line, re.IGNORECASE)]
        self.assertEqual(len(instructions), 1)
        self.assertRegex(instructions[0],
                         r"^FROM python:3\.12\.\d+-slim-trixie@sha256:[0-9a-f]{64}$")
        self.assertNotIn("${", instructions[0])
        self.assertNotIn("--platform", instructions[0])

    def test_base_reference_has_a_single_source_of_truth_in_the_dockerfile(self):
        # Documentation pages can go stale; the FROM line is the pin. The
        # comment block requires registry re-verification before any update.
        reference = next(line for line in self.instructions if line.startswith("FROM "))
        tag, digest = reference.removeprefix("FROM ").split("@")
        self.assertEqual(self.dockerfile.count(digest), 1)
        self.assertRegex(tag, r"^python:3\.12\.\d+-slim-trixie$")
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertIn("re-verify the index digest", self.dockerfile)

    def test_build_keeps_hash_checked_wheels_without_mutable_system_updates(self):
        run = "\n".join(line for line in self.instructions if line.startswith("RUN "))
        self.assertIn("--require-hashes", run)
        self.assertIn("--only-binary=:all:", run)
        self.assertIn("-r requirements.lock", run)
        self.assertNotRegex(run, r"\bapt(?:-get)?\s+(?:update|upgrade|install)\b")
        self.assertNotRegex(run, r"\bpip\s+install[^\n]*--upgrade\b")
        self.assertNotIn("--no-binary", run)


if __name__ == "__main__":
    unittest.main()
