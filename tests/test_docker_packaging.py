"""Docker packaging contracts; config rendering only, never start a container."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class DockerPackagingTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        self.service = self.compose["services"]["trendradar"]
        self.setup = self.compose["services"]["setup"]

    def test_service_reads_runtime_directory_not_env_snapshot(self):
        self.assertNotIn("env_file", self.service)
        self.assertEqual(self.service["environment"]["TRENDRADAR_RUNTIME_ENV"], "/app/runtime/env")
        self.assertIn("./runtime:/app/runtime:ro", self.service["volumes"])
        self.assertNotIn("./runtime/env:/app/runtime/env:ro", self.service["volumes"])
        self.assertEqual(self.service["environment"]["STORAGE_BACKEND"], "local")
        self.assertEqual(self.service["environment"]["DOCKER_CONTAINER"], "true")
        self.assertEqual(self.service["user"], "${TRENDRADAR_UID:-1000}:${TRENDRADAR_GID:-1000}")

    def test_setup_uses_explicit_runtime_mode_without_changing_native_default(self):
        self.assertNotIn("build", self.setup)
        self.assertIn("--runtime-config", self.setup["entrypoint"])
        self.assertIn("/setup/runtime/env", self.setup["entrypoint"])
        self.assertIn("docker", self.setup["entrypoint"])
        self.assertIn(".:/setup", self.setup["volumes"])
        self.assertEqual(self.setup["volumes"], [".:/setup"])
        self.assertEqual(self.setup["profiles"], ["setup"])
        self.assertEqual(self.setup["ports"], ["127.0.0.1:${SETUP_PORT:-8765}:8765"])

    def test_volume_initializer_is_separate_from_configuration(self):
        initializer = self.compose["services"]["volume-init"]
        self.assertEqual(initializer["profiles"], ["setup"])
        self.assertEqual(initializer["image"], self.setup["image"])
        self.assertEqual(initializer["user"], "0:0")
        self.assertEqual(initializer["network_mode"], "none")
        self.assertEqual(initializer["volumes"], ["trendradar-output:/app/output"])
        self.assertEqual(initializer["entrypoint"], ["python", "deploy/docker/manage.py", "init-volume"])
        self.assertNotIn("build", initializer)
        self.assertNotIn("ports", initializer)

    def test_image_uses_one_entrypoint_for_scheduler_and_doctor(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn('LABEL org.trendradar.runtime-config="1"', dockerfile)
        self.assertIn('ENTRYPOINT ["python", "deploy/docker/entrypoint.py"]', dockerfile)
        self.assertIn('CMD ["schedule"]', dockerfile)
        self.assertIn("CMD python deploy/docker/entrypoint.py doctor", dockerfile)
        self.assertIn("USER trendradar", dockerfile)
        self.assertIn("COPY .env.example ./", dockerfile)

    def test_private_runtime_files_are_excluded_from_build_and_git(self):
        ignored = set((ROOT / ".dockerignore").read_text().splitlines())
        self.assertTrue({"runtime", ".env", ".env.backups", "..env.backups"}.issubset(ignored))
        self.assertIn("/runtime/", (ROOT / ".gitignore").read_text().splitlines())
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            if line.startswith("COPY "):
                sources = line.split()[1:-1]
                self.assertNotIn("runtime", sources)
                self.assertNotIn(".env", sources)
                self.assertNotIn(".", sources)

    def test_compose_bootstrap_does_not_inject_legacy_app_secrets(self):
        if shutil.which("docker") is None:
            self.skipTest("Docker CLI is not installed")
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=10)
        if probe.returncode:
            self.skipTest("Docker Compose is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / "compose.yaml", root / "compose.yaml")
            (root / ".env").write_text(
                "TRENDRADAR_UID=12345\nTRENDRADAR_GID=12346\n"
                "EMAIL_PASSWORD=synthetic-legacy-secret\nAI_MODEL=openai/stale-model\n"
            )
            env = {"HOME": directory, "PATH": os.environ.get("PATH", os.defpath)}
            result = subprocess.run(
                ["docker", "compose", "--project-name", "runtime-packaging-test", "config", "--format", "json"],
                cwd=root, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = json.loads(result.stdout)["services"]["trendradar"]
            self.assertEqual(rendered["user"], "12345:12346")
            self.assertNotIn("EMAIL_PASSWORD", rendered["environment"])
            self.assertNotIn("AI_MODEL", rendered["environment"])
            self.assertNotIn("synthetic-legacy-secret", result.stdout)
            mounts = {item["target"]: item for item in rendered["volumes"]}
            self.assertTrue(mounts["/app/runtime"]["read_only"])
            self.assertEqual(mounts["/app/runtime"]["type"], "bind")


if __name__ == "__main__":
    unittest.main()
