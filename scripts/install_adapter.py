#!/usr/bin/env python3
"""Install a host adapter from this checkout (no GitHub release needed).

Development loop:

    # edit adapters/<host>/, then
    .venv/bin/python scripts/install_adapter.py --host dsh

It builds the release asset from ``adapters/<host>/`` exactly like
``build_adapter_release.py`` does, verifies it against its own manifest, renders
the bundle template for this machine, and writes the payload into the adapter
directory.

Only the installed copy is what a host loads — for DSH that is
``~/.dsh/paperdb-integration`` (symlinked from ``~/.agents/skills/my-paperdb``
and from the active profile's ``@local/dsh-paperdb-mcp``), so editing
``adapters/<host>/`` alone changes nothing until this script (or
``paperdb adapter update``) runs.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from paperdb import adapters, mcp_config  # noqa: E402
from paperdb.adapters import AdapterError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=adapters.DEFAULT_HOST,
        help=f"host adapter ({', '.join(adapters.list_hosts())})",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get(adapters.ADAPTER_DIR_ENV),
        help=f"install directory (default: ${adapters.ADAPTER_DIR_ENV}, the host's legacy "
        "variable, or the host default)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(mcp_config.BASE_URL_ENV, "https://library.shaoyuanyu.cn"),
    )
    parser.add_argument("--repo", default=adapters.repo_name())
    parser.add_argument("--version", help="override the pyproject version")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        adapter = adapters.get_adapter(args.host)
        target = adapters.resolve_target(adapter, args.target)
    except AdapterError as exc:
        print(f"install_adapter: {exc}", file=sys.stderr)
        return 2

    version = args.version
    if not version:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]

    with tempfile.TemporaryDirectory(prefix="paperdb-adapter-") as tmp:
        built = adapters.build_asset(
            adapter, ROOT, pathlib.Path(tmp), version=version, tag=f"v{version}"
        )
        asset_bytes = built["asset"].read_bytes()

    try:
        verified = adapters.verify_asset(adapter, asset_bytes, expected_version=version)
    except AdapterError as exc:
        print(f"install_adapter: {exc}", file=sys.stderr)
        return 1

    release = adapters.Release(
        version=version,
        tag=f"v{version}",
        published_at="",
        prerelease=False,
        notes="local checkout install",
        page_url="",
        asset_name=adapter.asset_name(version),
        asset_url="",
        asset_sha256=adapters.sha256_hex(asset_bytes),
        checksums_url=None,
    )
    context = adapters.build_context(target, base_url=args.base_url, repo=args.repo)
    try:
        report = adapters.install(
            adapter, target, verified, release, context=context, dry_run=args.dry_run
        )
    except AdapterError as exc:
        print(f"install_adapter: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
