"""``paperdb adapter`` — install/update the host adapter (MCP bundle + skill).

This lives on the command line, not on the MCP tool surface, for two reasons:

* it is host-specific (which host's config directory to write), while the tool
  surface must stay harness-neutral;
* it writes files outside the database, which is an owner action, not something
  a remote model should trigger mid-conversation.

Human output is the default; ``--json`` gives a stable machine-readable object.
Applying an update is gated behind the owner's current TOTP code, verified
against the same API the site uses — the gate that used to sit on the MCP tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx

from paperdb import adapters, mcp_config


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run ``coro`` to completion without disturbing a caller's event loop.

    ``asyncio.run`` clears the thread's current loop on exit, which breaks an
    embedder (or a test harness) that owns one. The CLI has no loop of its own,
    so it restores whatever was installed before.
    """
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    try:
        return asyncio.run(coro)
    finally:
        if previous is not None and not previous.is_closed():
            asyncio.set_event_loop(previous)


def _adapter(args: argparse.Namespace) -> adapters.Adapter:
    return adapters.get_adapter(getattr(args, "host", None))


def _target(adapter: adapters.Adapter, args: argparse.Namespace) -> Path:
    return adapters.resolve_target(adapter, getattr(args, "dir", None))


def _resolve(args: argparse.Namespace) -> tuple[adapters.Adapter, Path] | None:
    """Adapter + install directory, or a usage error already printed."""
    try:
        adapter = _adapter(args)
        return adapter, _target(adapter, args)
    except adapters.AdapterError as exc:
        if getattr(args, "json", False):
            _print_json({"ok": False, "error": str(exc)})
        else:
            print(f"paperdb adapter: {exc}", file=sys.stderr)
        return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))


async def _verify_totp(code: str | None) -> None:
    """Fail closed: an adapter write never happens without a verified code."""
    if not code or not code.strip():
        raise adapters.AdapterError(
            "a current 6-digit TOTP code is required to write the adapter (--totp)"
        )
    token = mcp_config.load_token()
    if not token:
        raise adapters.AdapterError(
            "no API token to verify TOTP with: set PAPERDB_MCP_TOKEN or PAPERDB_MCP_TOKEN_FILE"
        )
    async with _client() as client:
        response = await client.post(
            f"{mcp_config.base_url()}/v1/totp/verify",
            headers={"Authorization": f"Bearer {token}", "X-TOTP-Code": code.strip()},
        )
    if response.status_code >= 400:
        try:
            body = response.json()
            detail = body.get("detail") or body.get("title") or response.text
            error = body.get("code") or f"http_{response.status_code}"
        except ValueError:
            detail, error = response.text[:200], f"http_{response.status_code}"
        raise adapters.AdapterError(f"TOTP rejected ({error}): {detail}")


# --------------------------------------------------------------------------- #
# commands


def status(args: argparse.Namespace) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return 2
    adapter, target = resolved
    report = adapters.installed_status(adapter, target)
    if args.json:
        _print_json({"ok": True, **report})
        return 0
    version = report["version"] or "untracked (installed from a checkout)"
    print(f"host       {report['host']}")
    print(f"directory  {report['target']}")
    print(f"version    {version}")
    if report["packageVersion"]:
        print(f"package    {report['packageVersion']}")
    if report["installedAt"]:
        print(f"installed  {report['installedAt']}")
    for item in report["files"]:
        mark = "ok " if item["installed"] else "missing"
        drift = "" if not item["installed"] or item["matchesState"] else "  (locally edited)"
        print(f"  {mark}  {item['path']}{drift}")
    if report["drifted"]:
        print(f"drifted    {', '.join(report['drifted'])}")
    return 0


async def _check(adapter: adapters.Adapter, args: argparse.Namespace) -> dict[str, Any]:
    target = _target(adapter, args)
    installed = adapters.installed_status(adapter, target)
    token = adapters.github_token()
    async with _client() as client:
        release = await adapters.fetch_release(
            client, args.repo or adapters.repo_name(), adapter, token=token
        )
    current = installed["version"]
    newer = current is None or adapters.parse_version(release.version) > adapters.parse_version(
        current
    )
    return {
        "ok": True,
        "host": adapter.host,
        "target": str(target),
        "installed": {
            "version": current,
            "packageVersion": installed["packageVersion"],
            "tracked": installed["tracked"],
            "drifted": installed["drifted"],
        },
        "latest": {
            "version": release.version,
            "tag": release.tag,
            "publishedAt": release.published_at,
            "prerelease": release.prerelease,
            "page": release.page_url,
            "asset": release.asset_name,
            "sha256": release.asset_sha256,
            "notes": release.notes[:1200],
        },
        "updateAvailable": newer,
    }


def check(args: argparse.Namespace) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return 2
    adapter, _ = resolved
    try:
        report = _run(_check(adapter, args))
    except (adapters.AdapterError, httpx.HTTPError) as exc:
        payload = {"ok": False, "host": adapter.host, "error": str(exc)}
        if args.json:
            _print_json(payload)
        else:
            print(f"paperdb adapter check: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(report)
        return 0
    latest = report["latest"]
    current = report["installed"]["version"] or "untracked"
    print(f"host       {report['host']}  ({report['target']})")
    print(f"installed  {current}")
    print(f"latest     {latest['version']}  {latest['tag']}  {latest['page']}")
    print("status     " + ("update available" if report["updateAvailable"] else "up to date"))
    return 0


async def _update(adapter: adapters.Adapter, args: argparse.Namespace) -> dict[str, Any]:
    target = _target(adapter, args)
    if not args.dry_run:
        await _verify_totp(args.totp)
    token = adapters.github_token()
    async with _client() as client:
        release = await adapters.fetch_release(
            client, args.repo or adapters.repo_name(), adapter, version=args.version, token=token
        )
        installed = adapters.installed_status(adapter, target)
        current = installed["version"]
        if current is not None:
            current_key = adapters.parse_version(current)
            target_key = adapters.parse_version(release.version)
            if target_key < current_key:
                raise adapters.AdapterError(
                    f"{release.version} is older than the installed {current}; "
                    "use `paperdb adapter rollback` to revert"
                )
            if target_key == current_key and not args.version:
                return {
                    "ok": True,
                    "host": adapter.host,
                    "target": str(target),
                    "upToDate": True,
                    "version": current,
                    "release": release.tag,
                    "restartRequired": False,
                }
        payload = await adapters.download_bytes(client, release.asset_url, token=token)
        digest = release.asset_sha256 or await adapters.checksum_from_release(
            client, release, release.asset_name, token=token
        )
        if not digest:
            raise adapters.AdapterError(
                "release carries no SHA-256 digest (asset digest and SHA256SUMS are both "
                "missing); refusing to install unverified files"
            )
        verified = adapters.verify_asset(
            adapter, payload, expected_sha256=digest, expected_version=release.version
        )
    # The rendered bundle records the release being installed (the server
    # compares it with later releases for its update notices).
    context = adapters.build_context(
        target, base_url=args.base_url, repo=args.repo, version=release.version
    )
    return adapters.install(
        adapter, target, verified, release, context=context, dry_run=args.dry_run
    )


def update(args: argparse.Namespace) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return 2
    adapter, _ = resolved
    try:
        report = _run(_update(adapter, args))
    except (adapters.AdapterError, httpx.HTTPError) as exc:
        if args.json:
            _print_json({"ok": False, "host": adapter.host, "error": str(exc)})
        else:
            print(f"paperdb adapter update: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(report)
        return 0
    if report.get("upToDate"):
        print(f"paperdb adapter: {report['version']} is up to date ({report['release']})")
        return 0
    verb = "would change" if report["dryRun"] else "changed"
    print(
        f"paperdb adapter: {report['host']} {report['from'] or 'untracked'} -> "
        f"{report['to']}  ({report['target']})"
    )
    for item in report["assets"]:
        print(f"  {item['action']:<9} {item['path']}  ({item['bytes']} bytes)")
    changed = report.get("wouldChange") or report.get("changed") or []
    if changed:
        print(f"{verb}: {', '.join(changed)}")
    if report["restartRequired"]:
        host_hint = "DSH" if report["host"] == "dsh" else "the host"
        print(f"restart required: {host_hint} reloads the bundle only at startup")
    return 0


async def _rollback(adapter: adapters.Adapter, args: argparse.Namespace) -> dict[str, Any]:
    target = _target(adapter, args)
    await _verify_totp(args.totp)
    return adapters.rollback(adapter, target)


def rollback(args: argparse.Namespace) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return 2
    adapter, _ = resolved
    try:
        report = _run(_rollback(adapter, args))
    except (adapters.AdapterError, httpx.HTTPError) as exc:
        if args.json:
            _print_json({"ok": False, "host": adapter.host, "error": str(exc)})
        else:
            print(f"paperdb adapter rollback: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(report)
        return 0
    print(f"paperdb adapter: rolled back {report['host']} to {report['version']}")
    for path in report["restored"]:
        print(f"  restored {path}")
    for path in report["removed"]:
        print(f"  removed  {path}")
    if report["restartRequired"]:
        print("restart required: the host reloads the bundle only at startup")
    return 0


# --------------------------------------------------------------------------- #
# parser


def _add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host",
        metavar="HOST",
        help=f"agent host to manage (default: ${adapters.HOST_ENV} or {adapters.DEFAULT_HOST}; "
        f"known: {', '.join(adapters.list_hosts())})",
    )
    parser.add_argument(
        "--dir",
        metavar="PATH",
        help=f"install directory (default: ${adapters.ADAPTER_DIR_ENV}, the host's legacy "
        "variable, or the host default)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = sub.add_parser(
        "adapter",
        help="install or update a host adapter (the MCP bundle + skill that registers this server)",
    )
    commands = parser.add_subparsers(dest="adapter_command", required=True)

    status_parser = commands.add_parser(
        "status", help="installed adapter state, offline (version, files, local edits)"
    )
    _add_target_options(status_parser)
    status_parser.set_defaults(func=status)

    check_parser = commands.add_parser(
        "check", help="compare the installed adapter with the latest published release"
    )
    _add_target_options(check_parser)
    check_parser.add_argument("--repo", metavar="OWNER/NAME", help="GitHub repository")
    check_parser.set_defaults(func=check)

    update_parser = commands.add_parser(
        "update", help="download, verify and install the latest (or a pinned) adapter release"
    )
    _add_target_options(update_parser)
    update_parser.add_argument("--version", help="pin a release version (never downgrades)")
    update_parser.add_argument("--repo", metavar="OWNER/NAME", help="GitHub repository")
    update_parser.add_argument("--base-url", help="API URL written into the host bundle")
    update_parser.add_argument(
        "--totp", metavar="CODE", help="current 6-digit TOTP code (required unless --dry-run)"
    )
    update_parser.add_argument("--dry-run", action="store_true", help="preview, write nothing")
    update_parser.set_defaults(func=update)

    rollback_parser = commands.add_parser(
        "rollback", help="restore the adapter files from the last backup"
    )
    _add_target_options(rollback_parser)
    rollback_parser.add_argument("--totp", metavar="CODE", help="current 6-digit TOTP code")
    rollback_parser.set_defaults(func=rollback)
