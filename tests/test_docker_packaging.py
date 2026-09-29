"""Docker packaging contracts; config rendering only, never start a container."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

from deploy.docker.runtime_config import BASE_ENV_KEYS


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

    def test_all_services_share_fixed_version_and_multiarch_index(self):
        # Registry evidence (2026-09-28) identified this as the index, not either
        # platform child manifest. This offline contract does not requery GHCR.
        expected = "${TREND_RADAR_IMAGE:-ghcr.io/daxia9522/trendradar-lite-deploy:v26.9@sha256:b07e2424a0d5451d50c3f8e205636edb2ce63ec0061dc97c9ac5636bba038760}"
        for name, service in self.compose["services"].items():
            with self.subTest(service=name):
                self.assertEqual(service["image"], expected)
                self.assertNotIn(":latest", service["image"])
        self.assertIn("#TREND_RADAR_IMAGE=trendradar-lite-deploy:local", (ROOT / ".env.example").read_text())

    def test_capabilities_match_privileged_setup_filesystem_operations(self):
        for name, service in self.compose["services"].items():
            with self.subTest(service=name):
                self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
                self.assertEqual(service["cap_drop"], ["ALL"])
                self.assertFalse(service.get("privileged", False))
        self.assertNotIn("cap_add", self.service)
        self.assertEqual(set(self.setup["cap_add"]), {"CHOWN", "DAC_OVERRIDE", "FOWNER"})
        initializer = self.compose["services"]["volume-init"]
        self.assertEqual(set(initializer["cap_add"]), {"CHOWN", "DAC_READ_SEARCH"})
        # Do not claim whole-root read-only support before runtime validation.
        self.assertFalse(self.service.get("read_only", False))

    def test_image_uses_one_entrypoint_for_scheduler_and_doctor(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn('LABEL org.trendradar.runtime-config="1"', dockerfile)
        self.assertIn('ENTRYPOINT ["python", "deploy/docker/entrypoint.py"]', dockerfile)
        self.assertIn('CMD ["schedule"]', dockerfile)
        self.assertIn("CMD python deploy/docker/entrypoint.py doctor", dockerfile)
        self.assertIn("USER trendradar", dockerfile)
        self.assertIn("COPY .env.example ./", dockerfile)

    def test_optional_backup_shared_code_is_packaged_without_secrets_or_local_tools(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY deploy/*.py ./deploy/", dockerfile)
        self.assertTrue((ROOT / "deploy/backup_settings.py").is_file())
        self.assertTrue((ROOT / "deploy/r2_backup.py").is_file())
        for line in re.sub(r"\\\n", " ", dockerfile).splitlines():
            if line.startswith("ENV "):
                self.assertNotIn("S3_", line)
                self.assertNotIn("R2_BACKUP_", line)
            if line.startswith("COPY "):
                self.assertNotIn("local-tools", line)
                self.assertNotIn("runtime", line)
        for service in self.compose["services"].values():
            self.assertFalse(any(key.startswith(("S3_", "R2_BACKUP_")) for key in service.get("environment", {})))

    def test_image_uses_bundled_litellm_cost_map_through_runtime_environment(self):
        instructions = re.sub(r"\\\n", " ", (ROOT / "Dockerfile").read_text()).splitlines()
        env = next(line for line in instructions if line.startswith("ENV "))
        self.assertIn("LITELLM_LOCAL_MODEL_COST_MAP=True", env.split())
        # External runtime mode rebuilds the environment; the image value must survive it.
        self.assertIn("LITELLM_LOCAL_MODEL_COST_MAP", BASE_ENV_KEYS)

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
                "S3_ACCESS_KEY_ID=synthetic-legacy-access\nS3_SECRET_ACCESS_KEY=synthetic-legacy-s3-secret\n"
                "R2_BACKUP_ENABLED=true\n"
            )
            env = {"HOME": directory, "PATH": os.environ.get("PATH", os.defpath)}
            result = subprocess.run(
                ["docker", "compose", "--profile", "setup", "--project-name", "runtime-packaging-test", "config", "--format", "json"],
                cwd=root, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            services = json.loads(result.stdout)["services"]
            rendered = services["trendradar"]
            self.assertEqual(rendered["user"], "12345:12346")
            self.assertEqual(rendered["security_opt"], ["no-new-privileges:true"])
            self.assertEqual(rendered["cap_drop"], ["ALL"])
            self.assertEqual(rendered["image"], self.service["image"].removeprefix("${TREND_RADAR_IMAGE:-").removesuffix("}"))
            self.assertNotIn("EMAIL_PASSWORD", rendered["environment"])
            self.assertNotIn("AI_MODEL", rendered["environment"])
            self.assertNotIn("synthetic-legacy-secret", result.stdout)
            self.assertNotIn("synthetic-legacy-access", result.stdout)
            self.assertNotIn("synthetic-legacy-s3-secret", result.stdout)
            self.assertNotIn("R2_BACKUP_ENABLED", rendered["environment"])
            mounts = {item["target"]: item for item in rendered["volumes"]}
            self.assertTrue(mounts["/app/runtime"]["read_only"])
            self.assertEqual(mounts["/app/runtime"]["type"], "bind")
            for name in ("setup", "volume-init"):
                self.assertEqual(services[name]["image"], rendered["image"])
                self.assertEqual(services[name]["security_opt"], ["no-new-privileges:true"])
                self.assertEqual(services[name]["cap_drop"], ["ALL"])
                self.assertEqual(set(services[name]["cap_add"]), set(self.compose["services"][name]["cap_add"]))
            # Local source builds need an explicitly writable tag, not an OCI
            # digest. Rendering proves all helper services share that override;
            # it never builds, pulls, creates volumes or starts containers.
            with (root / ".env").open("a") as stream:
                stream.write("TREND_RADAR_IMAGE=trendradar-lite-deploy:local\n")
            local = subprocess.run(
                ["docker", "compose", "--profile", "setup", "config", "--format", "json"],
                cwd=root, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(local.returncode, 0, local.stderr)
            self.assertTrue(all(service["image"] == "trendradar-lite-deploy:local"
                                for service in json.loads(local.stdout)["services"].values()))


if __name__ == "__main__":
    unittest.main()
