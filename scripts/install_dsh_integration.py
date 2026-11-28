#!/usr/bin/env python3
"""Install the DSH integration from this checkout (no GitHub release needed).

Development loop:

    # edit for-agents/, then
    .venv/bin/python scripts/install_dsh_integration.py

It builds the release asset from ``for-agents/`` exactly like
``build_dsh_release.py`` does, verifies it against its own manifest, renders
the bundle template for this machine, and writes both the bundle and the skill
into the integration directory (``PAPERDB_DSH_INTEGRATION_DIR``).

Only the installed copy is what DSH loads: ``~/.agents/skills/my-paperdb`` and
the profile's ``@local/dsh-paperdb-mcp`` link both point into that directory,
so editing ``for-agents/`` alone changes nothing until this script (or a
release update) runs.
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

from paperdb import dsh_integration as dsh  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=os.environ.get(dsh.INTEGRATION_DIR_ENV),
        help=f"integration directory (default: ${dsh.INTEGRATION_DIR_ENV})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PAPERDB_MCP_BASE_URL", "https://library.shaoyuanyu.cn"),
    )
    parser.add_argument("--repo", default=os.environ.get(dsh.REPO_ENV) or dsh.DEFAULT_REPO)
    parser.add_argument("--version", help="override the pyproject version")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.target:
        print(f"install_dsh_integration: ${dsh.INTEGRATION_DIR_ENV} is not set", file=sys.stderr)
        return 2
    target = dsh.resolve_target(args.target)

    version = args.version
    if not version:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]

    with tempfile.TemporaryDirectory(prefix="paperdb-dsh-") as tmp:
        built = dsh.build_release(ROOT, pathlib.Path(tmp), version=version, tag=f"v{version}")
        asset_bytes = built["asset"].read_bytes()

    try:
        verified = dsh.verify_asset(asset_bytes, expected_version=version)
    except dsh.IntegrationError as exc:
        print(f"install_dsh_integration: {exc}", file=sys.stderr)
        return 1

    release = dsh.Release(
        version=version,
        tag=f"v{version}",
        published_at="",
        prerelease=False,
        notes="local checkout install",
        page_url="",
        asset_name=f"paperdb-dsh-{version}.tar.gz",
        asset_url="",
        asset_sha256=dsh.sha256_hex(asset_bytes),
        checksums_url=None,
    )
    context = {
        "mcp_command": dsh.default_mcp_command(),
        "base_url": args.base_url,
        # The bundle no longer embeds the token, but the renderer still needs
        # every placeholder it may encounter.
        "token": "(not used: the server reads PAPERDB_MCP_TOKEN_FILE)",
        "target": str(target),
        "repo": args.repo,
    }
    try:
        report = dsh.install(target, verified, release, context=context, dry_run=args.dry_run)
    except dsh.IntegrationError as exc:
        print(f"install_dsh_integration: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
