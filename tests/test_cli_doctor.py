import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DoctorSmokeTests(unittest.TestCase):
    def test_doctor_runs_with_repository_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workdir = Path(temp_dir)
            shutil.copytree(ROOT / "config", workdir / "config")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            for key in list(env):
                if key.startswith(("AI_", "EMAIL_", "S3_")):
                    env.pop(key)

            result = subprocess.run(
                [sys.executable, "-m", "trendradar", "--doctor"],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("体检通过", result.stdout)
