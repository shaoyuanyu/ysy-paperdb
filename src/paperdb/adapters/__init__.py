"""Host adapters for this MCP server.

The server is harness-neutral; a host is only ever mentioned here and in
``adapters/<host>/``.  Everything else in the package — including the MCP tool
surface — must stay host-agnostic.

Usage::

    from paperdb.adapters import get_adapter

    adapter = get_adapter()            # PAPERDB_ADAPTER_HOST or "dsh"
    adapter = get_adapter("dsh")
"""

from __future__ import annotations

import os

from paperdb.adapters.base import (
    ADAPTER_DIR_ENV,
    HOST_ENV,
    Adapter,
    AdapterError,
    Release,
    VerifiedAsset,
    build_asset,
    build_context,
    checksum_from_release,
    download_bytes,
    fetch_release,
    github_token,
    install,
    installed_status,
    parse_version,
    release_url_repo,
    render_bundle,
    repo_name,
    resolve_target,
    rollback,
    sha256_hex,
    verify_asset,
)
from paperdb.adapters.dsh import DSH

DEFAULT_HOST = "dsh"

#: Every host this release can install into.  Adding one here plus a payload
#: directory at ``adapters/<host>/`` is the whole extension point.
ADAPTERS: dict[str, Adapter] = {DSH.host: DSH}


def list_hosts() -> list[str]:
    return sorted(ADAPTERS)


def get_adapter(host: str | None = None) -> Adapter:
    name = (host or os.environ.get(HOST_ENV) or DEFAULT_HOST).strip().lower()
    try:
        return ADAPTERS[name]
    except KeyError:
        known = ", ".join(list_hosts())
        raise AdapterError(f"unknown host {name!r}; known hosts: {known}") from None


__all__ = [
    "ADAPTERS",
    "ADAPTER_DIR_ENV",
    "DEFAULT_HOST",
    "HOST_ENV",
    "Adapter",
    "AdapterError",
    "Release",
    "VerifiedAsset",
    "build_asset",
    "build_context",
    "checksum_from_release",
    "download_bytes",
    "fetch_release",
    "get_adapter",
    "github_token",
    "install",
    "installed_status",
    "list_hosts",
    "parse_version",
    "release_url_repo",
    "render_bundle",
    "repo_name",
    "resolve_target",
    "rollback",
    "sha256_hex",
    "verify_asset",
]
