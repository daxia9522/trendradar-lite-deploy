#!/usr/bin/env python3
"""Validated image metadata for tag releases and main-only same-version rebuilds."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import sys
import tomllib


def resolve_tag(root: Path, ref: str, event: str) -> str:
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    source = ast.parse((root / 'trendradar/__init__.py').read_text())
    declared = [ast.literal_eval(node.value) for node in source.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)]
    if not isinstance(version, str) or declared != [version]:
        raise ValueError('package version declarations must agree')
    tag = 'v' + version
    if re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag) is None:
        raise ValueError('package version is not a valid image tag')
    expected_ref = 'refs/heads/main' if event == 'workflow_dispatch' else f'refs/tags/{tag}' if event == 'push' else None
    if expected_ref is None or ref != expected_ref:
        raise ValueError('release event/ref does not match the package version or main rebuild policy')
    return tag


def main() -> int:
    try:
        tag = resolve_tag(Path(__file__).resolve().parents[2], os.environ.get('GITHUB_REF', ''),
                          os.environ.get('GITHUB_EVENT_NAME', ''))
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        print('Release metadata validation failed; no image tags were emitted.', file=sys.stderr)
        return 1
    print(f'image_tag={tag}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
