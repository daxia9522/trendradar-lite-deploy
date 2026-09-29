#!/usr/bin/env python3
"""Validate GitHub workflow files: YAML parse + bash -n on embedded run scripts,
plus offline simulation of the stale-guard logic."""
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

WF_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}" + (f" :: {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run_guard_script(created_iso: str, run_attempt: str = "1", stale_minutes: str = "30") -> str:
    """Run the guard bash body with mocked env and a fake `gh`/`date`-compatible setup."""
    body = GUARD_BODY
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(body)
        path = f.name
    env = {
        "PATH": f"{MOCK_BIN}:/usr/bin:/bin",
        "GITHUB_REPOSITORY": "daxia9522/trendradar-lite",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": run_attempt,
        "GITHUB_OUTPUT": MOCK_OUTPUT,
        "STALE_MINUTES": stale_minutes,
        "GH_TOKEN": "dummy",
    }
    proc = subprocess.run(["bash", path], env=env, capture_output=True, text=True)
    out = Path(MOCK_OUTPUT).read_text() if Path(MOCK_OUTPUT).exists() else ""
    return f"rc={proc.returncode} stdout={proc.stdout.strip()!r} output={out.strip()!r} stderr={proc.stderr.strip()!r}"


wf_files = sorted(WF_DIR.glob("*.yml"))
check(
    "deployment workflow files exist",
    all((WF_DIR / name).exists() for name in ("crawler.yml", "weekly-report.yml")),
    str(wf_files),
)

parsed = {}
for wf in wf_files:
    try:
        data = yaml.safe_load(wf.read_text())
        parsed[wf.name] = data
        check(f"yaml parse {wf.name}", True)
    except Exception as e:  # noqa: BLE001
        check(f"yaml parse {wf.name}", False, str(e))

# Supply-chain contracts are offline: SHA/tag resolution is reviewed separately
# against the official API; syntax alone is not proof of trusted code/signatures.
for wf in wf_files:
    text = wf.read_text()
    check(f"{wf.name} top-level permissions are read-only",
          all(value == "read" for value in parsed[wf.name].get("permissions", {}).values()))
    for line in text.splitlines():
        if re.search(r"\buses:", line):
            check(f"{wf.name} action has full SHA and original tag: {line.strip()}",
                  re.fullmatch(r"\s*(?:-\s+)?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\S+\s*", line) is not None)
    for job_name, job in parsed[wf.name]["jobs"].items():
        if wf.name != "release-image.yml":
            check(f"{wf.name}:{job_name} does not grant publishing/signing permissions",
                  all(value == "read" for value in job.get("permissions", {}).values()))
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if uses.startswith("actions/checkout@"):
                check(f"{wf.name}:{job_name} checkout does not persist credentials",
                      step.get("with", {}).get("persist-credentials") is False)
            if uses.startswith("actions/setup-python@"):
                options = step.get("with", {})
                check(f"{wf.name}:{job_name} Python cache follows lock file",
                      options.get("cache") == "pip" and options.get("cache-dependency-path") == "requirements.lock")
            for line in step.get("run", "").splitlines():
                if re.search(r"\bpip(?:\d+)?\b.*\binstall\b", line):
                    check(f"{wf.name}:{job_name} pip install requires hashes",
                          line.strip() == "python -m pip install --require-hashes -r requirements.lock")

release = parsed["release-image.yml"]
release_job = release["jobs"]["image"]
release_steps = release_job["steps"]
build = next(step for step in release_steps if step.get("uses", "").startswith("docker/build-push-action@"))
attest = next(step for step in release_steps if step.get("uses", "").startswith("actions/attest-build-provenance@"))
check("release image job grants only required publishing/signing scopes", release_job.get("permissions") == {
    "contents": "read", "packages": "write", "id-token": "write", "attestations": "write",
})
check("release attests only after successful multi-platform push",
      release_steps.index(attest) > release_steps.index(build)
      and build.get("id") == "build" and build["with"].get("push") is True
      and set(build["with"]["platforms"].split(",")) == {"linux/amd64", "linux/arm64"}
      and "if" not in attest and not attest.get("continue-on-error", False))
check("release attests actual build digest, not mutable tag", attest.get("with") == {
    "subject-name": "${{ steps.version.outputs.image_name }}",
    "subject-digest": "${{ steps.build.outputs.digest }}",
    "push-to-registry": True,
})
metadata = next(step for step in release_steps if step.get("id") == "meta")
check("release metadata and attestation share normalized untagged name",
      metadata["with"]["images"] == attest["with"]["subject-name"])

# structural checks
for name in ("crawler.yml", "weekly-report.yml"):
    data = parsed.get(name, {})
    jobs = data.get("jobs", {})
    check(f"{name} has guard job", "guard" in jobs)
    check(f"{name} guard outputs skip", jobs.get("guard", {}).get("outputs", {}).get("skip") is not None)
    main_job = [j for j in jobs if j != "guard"]
    for j in main_job:
        check(f"{name} job '{j}' needs guard + conditional skip",
              jobs[j].get("needs") == "guard" and "skip != 'true'" in jobs[j].get("if", ""))
    perms = data.get("permissions", {})
    check(f"{name} permissions actions:read", perms.get("actions") == "read")

# extract bash bodies and bash -n
for wf in wf_files:
    data = parsed[wf.name]
    for jname, job in data.get("jobs", {}).items():
        for i, step in enumerate(job.get("steps", [])):
            run = step.get("run")
            if not run:
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
                f.write(run)
                p = f.name
            r = subprocess.run(["bash", "-n", p], capture_output=True, text=True)
            check(f"bash -n {wf.name}:{jname}:step{i}", r.returncode == 0, r.stderr.strip())

# ci.yml must parse every script: `bash -n a b` parses only a (b becomes $1).
CI_SHELL_CHECK = next(step["run"] for step in parsed["ci.yml"]["jobs"]["python"]["steps"]
                      if step.get("name") == "Check shell scripts")
REPO = WF_DIR.parent.parent
with tempfile.TemporaryDirectory() as tmp:
    tree = Path(tmp)
    scripts = [Path("install.sh")] + [path.relative_to(REPO) for name in ("linux", "docker")
                                      for path in sorted((REPO / "deploy" / name).glob("*.sh"))]
    for script in scripts:
        (tree / script).parent.mkdir(parents=True, exist_ok=True)
        (tree / script).write_text((REPO / script).read_text())
    (tree / "ci-step.sh").write_text(CI_SHELL_CHECK)
    # Actions runs a `run:` step without an explicit shell as `bash -e {0}`.
    r = subprocess.run(["bash", "-e", "ci-step.sh"], cwd=tree, capture_output=True, text=True)
    check("ci.yml shell check passes on repository scripts", r.returncode == 0, r.stderr.strip())
    (tree / scripts[-1]).write_text((tree / scripts[-1]).read_text() + "\nif then\n")
    r = subprocess.run(["bash", "-e", "ci-step.sh"], cwd=tree, capture_output=True, text=True)
    check(f"ci.yml shell check catches a syntax error in {scripts[-1]}", r.returncode != 0)

# --- offline simulation of guard logic ---
GUARD_BODY = parsed["crawler.yml"]["jobs"]["guard"]["steps"][0]["run"]

MOCK_DIR = Path(tempfile.mkdtemp())
MOCK_BIN = str(MOCK_DIR / "bin")
MOCK_OUTPUT = str(MOCK_DIR / "github_output")
Path(MOCK_BIN).mkdir(parents=True, exist_ok=True)

now = datetime.now(timezone.utc)


def write_mock_gh(created_dt: datetime) -> None:
    Path(MOCK_BIN, "gh").write_text(
        "#!/bin/bash\n"
        'if [ "$4" = ".created_at" ]; then echo "' + created_dt.strftime("%Y-%m-%dT%H:%M:%SZ") + '"; exit 0; fi\n'
        'echo "{}" >&2; exit 1\n'
    )
    Path(MOCK_BIN, "gh").chmod(0o755)


def reset_output() -> None:
    Path(MOCK_OUTPUT).unlink(missing_ok=True)


# case 1: fresh dispatch (created 60s ago) -> skip=false
write_mock_gh(now - timedelta(seconds=60))
reset_output()
r = run_guard_script("x")
check("guard fresh run proceeds", "skip=false" in r, r)

# case 2: zombie revived (created 3 days ago) -> skip=true
write_mock_gh(now - timedelta(days=3))
reset_output()
r = run_guard_script("x")
check("guard stale run skips", "skip=true" in r, r)

# case 3: manual re-run (attempt 2) of old run -> skip=false
write_mock_gh(now - timedelta(days=3))
reset_output()
r = run_guard_script("x", run_attempt="2")
check("guard re-run proceeds", "skip=false" in r, r)

# case 4: gh api broken -> fail-open proceed
Path(MOCK_BIN, "gh").write_text("#!/bin/bash\nexit 1\n")
Path(MOCK_BIN, "gh").chmod(0o755)
reset_output()
r = run_guard_script("x")
check("guard gh failure fails open", "skip=false" in r and "fail-open" in r, r)

# case 5: boundary — 25 min old with 30 min threshold -> proceed
write_mock_gh(now - timedelta(minutes=25))
reset_output()
r = run_guard_script("x")
check("guard 25min/30min boundary proceeds", "skip=false" in r, r)

print()
if failures:
    print(f"RESULT: {len(failures)} failure(s): {failures}")
    sys.exit(1)
print("RESULT: all checks passed")
