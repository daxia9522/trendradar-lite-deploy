#!/usr/bin/env python3
"""Regenerate the wheel-only, universal runtime lock with isolated uv tooling."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = "0.12.19"
INDEX = "https://pypi.org/simple"


def compile_lock(root: Path, uv: str, *, upgrade: bool = False) -> None:
    # Ignore user resolver/index settings; no user cache or Python download is used.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("UV_", "PIP_"))}
    version = subprocess.check_output([uv, "--version"], env=environment, text=True)
    if version.split()[:2] != ["uv", UV_VERSION]:
        raise ValueError(f"Expected uv {UV_VERSION}, got {version.strip()!r}")
    source = root / "requirements.txt"
    destination = root / "requirements.lock"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="trendradar-lock-", dir="/tmp") as temporary:
        candidate = Path(temporary) / "requirements.lock"
        if destination.exists() and not upgrade:
            shutil.copyfile(destination, candidate)
        command = [
            uv, "--no-config", "pip", "compile", "requirements.txt",
            "--universal", "--python-version", "3.10", "--generate-hashes",
            "--no-strip-markers", "--only-binary", ":all:", "--emit-build-options",
            "--default-index", INDEX, "--no-python-downloads",
            "--cache-dir", str(Path(temporary) / "cache"),
            "--custom-compile-command", "python3 deploy/lock_dependencies.py",
            "--output-file", str(candidate),
        ]
        if upgrade:
            command.append("--upgrade")
        subprocess.run(command, cwd=root, env=environment, check=True, stdout=subprocess.DEVNULL)
        content = candidate.read_text(encoding="utf-8")
        if "--only-binary :all:\n" not in content or "--hash=sha256:" not in content:
            raise ValueError("Compiler did not produce a wheel-only hash lock")
        provenance = (
            f"# requirements.txt sha256: {digest}\n"
            f"# Resolver: uv {UV_VERSION}; Python >=3.10; universal; index: {INDEX}\n"
            "# Maintain via deploy/lock_dependencies.py; review and test updates before deployment.\n"
        )
        # Publish only a successful result, atomically on the destination filesystem.
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root,
                                             prefix=".requirements.lock-", delete=False) as output:
                pending = Path(output.name)
                output.write(provenance + content)
            pending.chmod(0o644)
            os.replace(pending, destination)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help=f"uv {UV_VERSION} executable from a /tmp venv")
    parser.add_argument("--upgrade", action="store_true", help="Re-resolve versions instead of retaining existing pins")
    arguments = parser.parse_args()
    try:
        compile_lock(ROOT, arguments.uv, upgrade=arguments.upgrade)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Lock generation failed; the existing lock was preserved: {error}\n")


if __name__ == "__main__":
    main()
