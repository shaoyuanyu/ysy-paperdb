#!/usr/bin/env python3
"""Build the DSH integration release asset.

Run in CI (or locally):

    uv run python scripts/build_dsh_release.py --out dist

Writes ``dist/paperdb-dsh-<version>.tar.gz`` and ``dist/manifest.json`` from
``for-agents/`` plus the version in ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from paperdb.dsh_integration import IntegrationError, build_release  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    parser.add_argument("--version", help="override the pyproject version")
    parser.add_argument("--tag", help="release tag (default: v<version>)")
    args = parser.parse_args()

    version = args.version
    if not version:
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = manifest["project"]["version"]
    try:
        paths = build_release(ROOT, ROOT / args.out, version=version, tag=args.tag)
    except IntegrationError as exc:
        print(f"build_dsh_release: {exc}", file=sys.stderr)
        return 1
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
