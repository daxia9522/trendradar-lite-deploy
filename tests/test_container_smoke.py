"""Exercise the CI smoke driver with a fake Docker CLI, not real containers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/container_smoke.sh"


class ContainerSmokeTests(unittest.TestCase):
    def run_smoke(self, fail_command=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls"
            docker = root / "docker"
            docker.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[0] == "run":
    Path(args[args.index("--cidfile") + 1]).write_text("a" * 64)
    if os.environ.get("FAIL_COMMAND") and os.environ["FAIL_COMMAND"] in args:
        raise SystemExit(7)
''')
            docker.chmod(0o755)
            environment = {"PATH": str(root) + os.pathsep + os.defpath, "HOME": directory,
                           "CALLS": str(log), "FAIL_COMMAND": fail_command, "TMPDIR": directory}
            result = subprocess.run(["bash", str(SCRIPT), "synthetic:ci"], env=environment,
                                    capture_output=True, text=True, timeout=15)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["calls", "docker"])
            return result, calls

    def test_ci_runs_smoke_after_build(self):
        steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["container"]["steps"]
        build = next(i for i, step in enumerate(steps) if step.get("name") == "Build image")
        smoke = next(i for i, step in enumerate(steps) if "container_smoke.sh" in step.get("run", ""))
        self.assertGreater(smoke, build)
        self.assertNotIn("continue-on-error", steps[smoke])

    def test_real_entrypoint_commands_are_network_isolated(self):
        result, calls = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 5)
        self.assertEqual([call[call.index("synthetic:ci") + 1:] for call in calls],
                         [["config-check"], ["doctor"], ["current", "--help"],
                          ["weekly", "--help"], ["show-schedule"]])
        for call in calls:
            self.assertEqual(call[call.index("--network") + 1], "none")
            self.assertEqual(call[call.index("--cap-drop") + 1], "ALL")
            self.assertNotIn("--env-file", call)
            self.assertNotIn("--volume", call)
            self.assertIn("--rm", call)

    def test_failure_stops_remaining_tests_and_removes_only_created_container(self):
        result, calls = self.run_smoke("doctor")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1], ["rm", "--force", "a" * 64])
