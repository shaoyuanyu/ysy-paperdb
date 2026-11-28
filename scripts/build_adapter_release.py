#!/usr/bin/env python3
"""Build one host adapter's release asset.

Run in CI (or locally):

    uv run python scripts/build_adapter_release.py --host dsh --out dist

Writes ``dist/paperdb-adapter-<host>-<version>.tar.gz`` plus ``dist/manifest.json``
from ``adapters/<host>/`` and the version in ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from paperdb import adapters  # noqa: E402
from paperdb.adapters import AdapterError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=adapters.DEFAULT_HOST,
        help=f"host adapter ({', '.join(adapters.list_hosts())})",
    )
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    parser.add_argument("--version", help="override the pyproject version")
    parser.add_argument("--tag", help="release tag (default: v<version>)")
    args = parser.parse_args()

    version = args.version
    if not version:
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = manifest["project"]["version"]
    try:
        adapter = adapters.get_adapter(args.host)
        paths = adapters.build_asset(adapter, ROOT, ROOT / args.out, version=version, tag=args.tag)
    except AdapterError as exc:
        print(f"build_adapter_release: {exc}", file=sys.stderr)
        return 1
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
