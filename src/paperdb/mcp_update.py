"""Update notices in MCP tool output (harness-neutral).

The server never installs anything — updates stay owner CLI actions — but it
can *report* that a newer release exists, so the agent can offer the update
to the user at a natural moment instead of the user having to ask.

How this stays cheap and neutral:

* the check hits ``https://api.github.com/repos/<repo>/releases/latest`` at
  most once per TTL (default 6 h).  Between checks a small JSON file under the
  MCP cache root is the source of truth, so a restarted server does not
  re-check either.  HTTP 404 (no published release) is a valid, cacheable
  "no update" answer, not an error; real failures cache a shorter backoff so
  a dead network path never becomes a request storm.
* the check runs as a background task scheduled by the first tool call after
  the cache goes stale; it never delays a tool response.
* "installed" comes from ``PAPERDB_MCP_ADAPTER_VERSION`` — rendered into the
  host bundle at install time, i.e. the release the skill+bundle came from —
  falling back to the installed ``paperdb`` distribution.  Nothing here names
  a host.
* a notice is delivered at most once per server process (per distinct latest
  version), spliced into the next tool result as an ``updateNotice`` field.

Configuration (environment):
    PAPERDB_MCP_ADAPTER_VERSION   installed adapter release (set by the bundle)
    PAPERDB_MCP_UPDATE_TTL        seconds between real checks (default 21600,
                                  0 disables notices and checks entirely)
    PAPERDB_UPDATE_REPO           GitHub repository (see paperdb.adapters)
    PAPERDB_GITHUB_TOKEN / GITHUB_TOKEN    raise the API rate limit
    PAPERDB_MCP_CACHE_DIR         cache root (default ~/.cache/paperdb/mcp)

The agent-facing protocol (ask the user, wait ~60 s, skip on silence) is
defined by the host skill, not here; the notice carries a short neutral hint.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from paperdb import page_cache
from paperdb.adapters import AdapterError, github_token, parse_version, repo_name
from paperdb.adapters.base import ASSET_RE, version_text

VERSION_ENV = "PAPERDB_MCP_ADAPTER_VERSION"
TTL_ENV = "PAPERDB_MCP_UPDATE_TTL"
NOTICE_KEY = "updateNotice"
MARKER = "[paperdb-update]"

DEFAULT_TTL = 6 * 3600
#: A failed check is retried after at most this long, so an unreachable API
#: never turns into a request storm (and never silences notices for long).
FAILURE_TTL = 30 * 60
#: Minimum backoff for failed checks, even when the configured TTL is tiny.
FAILURE_TTL_MIN = 5 * 60
NOTES_LIMIT = 600

_GITHUB_API = "https://api.github.com"

_INSTRUCTIONS = (
    "A newer paperdb release is available. Ask the user whether to update now, "
    "following the update procedure the host's my-paperdb skill documents; if "
    "the user has not replied after about 60 seconds, skip the update and "
    "carry on with the current task, and do not ask again this session. Never "
    "start an update without the user's confirmation."
)

#: In-memory state, reset by :func:`reset` (tests).
_task: asyncio.Task[None] | None = None
_notified: set[str] = set()


class _Unset:
    """Sentinel distinguishing "not resolved yet" from "resolved to None"."""


_UNSET = _Unset()
_installed: Any = _UNSET


# --------------------------------------------------------------------------- #
# configuration


def ttl_seconds() -> int:
    """Seconds between real update checks; 0 disables the feature."""
    raw = os.environ.get(TTL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TTL
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return DEFAULT_TTL


def enabled() -> bool:
    return ttl_seconds() > 0


def installed_version() -> str | None:
    """The release this integration came from; ``None`` when unknowable.

    Priority: the bundle-rendered ``PAPERDB_MCP_ADAPTER_VERSION`` (tracks the
    installed skill+bundle exactly, which is what ``paperdb adapter check``
    compares), then the installed ``paperdb`` distribution, then the package's
    ``__version__``.  ``None`` disables notices rather than guessing.
    """
    global _installed
    if _installed is _UNSET:
        env = os.environ.get(VERSION_ENV, "").strip()
        if env:
            _installed = env
        else:
            try:
                from importlib.metadata import version

                _installed = version("paperdb")
            except Exception:
                from paperdb import __version__

                _installed = __version__
    return _installed


def cache_path() -> Path:
    return page_cache.cache_root() / "update-check.json"


# --------------------------------------------------------------------------- #
# the cached check


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_cache() -> dict[str, Any] | None:
    try:
        entry = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def _write_cache(entry: dict[str, Any]) -> None:
    """Atomic, best-effort: notices are optional and must never break tools."""
    path = cache_path()
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write((json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        return
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fresh(entry: dict[str, Any], ttl: int) -> bool:
    """Is a cached entry still usable at this TTL?

    Successful checks (including "no published release") stay fresh for the
    full TTL; failed checks back off to a shorter window so a transient error
    is retried without waiting out the whole TTL.
    """
    checked = entry.get("checkedAt")
    if not isinstance(checked, str):
        return False
    try:
        checked_at = datetime.fromisoformat(checked)
    except ValueError:
        return False
    age = (datetime.now(UTC) - checked_at).total_seconds()
    window = min(max(ttl, FAILURE_TTL_MIN), FAILURE_TTL) if entry.get("error") else max(ttl, 1)
    return 0 <= age < window


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "paperdb-mcp-update-check",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_latest(client: httpx.AsyncClient) -> dict[str, Any]:
    """One GitHub API call -> cache entry fields; raises on failure.

    Deliberately mirrors ``paperdb.adapter check``'s notion of a usable
    release: the tag parses as a version and the release ships an adapter
    asset for *some* host (which host is the installer's business, not this
    server's).
    """
    repo = repo_name()
    response = await client.get(
        f"{_GITHUB_API}/repos/{repo}/releases/latest", headers=_github_headers()
    )
    if response.status_code == 404:
        return {"repo": repo, "latest": None}  # no published release: not an error
    if response.status_code >= 400:
        raise AdapterError(f"GitHub API returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AdapterError("GitHub API returned a non-JSON response") from exc
    tag = str(payload.get("tag_name") or "")
    latest = version_text(tag)
    try:
        parse_version(latest)
    except AdapterError as exc:
        raise AdapterError(f"release tag is not a version: {tag!r}") from exc
    assets = [item for item in (payload.get("assets") or []) if isinstance(item, dict)]
    has_asset = any(
        (match := ASSET_RE.match(str(item.get("name") or ""))) and match.group("version") == latest
        for item in assets
    )
    if not has_asset:
        raise AdapterError(f"release {tag} ships no adapter asset for any host")
    return {
        "repo": repo,
        "latest": latest,
        "tag": tag,
        "publishedAt": str(payload.get("published_at") or ""),
        "page": str(payload.get("html_url") or ""),
        "notes": str(payload.get("body") or "")[:NOTES_LIMIT],
    }


async def refresh() -> dict[str, Any]:
    """Run the real check and cache the result; returns the new entry.

    Failures are cached too (with the shorter backoff window), so a broken
    network does not retry on every tool call.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            fields = await _fetch_latest(client)
            entry = {"checkedAt": _now_iso(), **fields}
    except (AdapterError, httpx.HTTPError) as exc:
        detail = str(exc) or "no detail"
        entry = {
            "checkedAt": _now_iso(),
            "repo": repo_name(),
            "error": f"{type(exc).__name__}: {detail}",
        }
    _write_cache(entry)
    return entry


async def _refresh_quietly() -> None:
    with contextlib.suppress(Exception):
        await refresh()


def _schedule_refresh() -> None:
    """Fire-and-forget: refresh at most one task at a time, never blocking."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no loop (should not happen: tools are async)
        return
    _task = loop.create_task(_refresh_quietly())


# --------------------------------------------------------------------------- #
# notice assembly and delivery


def pending_notice() -> dict[str, Any] | None:
    """The notice to deliver with the next tool result, or ``None``.

    Reads the cache (scheduling a background refresh when it is stale) and
    returns the notice at most once per process for a given latest version.
    """
    if not enabled():
        return None
    ttl = ttl_seconds()
    entry = _load_cache()
    if entry is None or not _fresh(entry, ttl):
        _schedule_refresh()
    if not entry or entry.get("error") or entry.get("latest") is None:
        return None
    latest = str(entry["latest"])
    installed = installed_version()
    if not installed:
        return None
    try:
        newer = parse_version(latest) > parse_version(installed)
    except AdapterError:
        return None
    if not newer or latest in _notified:
        return None
    _notified.add(latest)
    return {
        "installed": installed,
        "latest": latest,
        "tag": entry.get("tag"),
        "publishedAt": entry.get("publishedAt"),
        "page": entry.get("page"),
        "notes": entry.get("notes") or "",
        "instructions": _INSTRUCTIONS,
    }


def _inject(text: str, notice: dict[str, Any]) -> str:
    """Attach the notice to a tool result without breaking its shape.

    JSON objects gain an ``updateNotice`` field; anything else (arrays, plain
    text) gets a clearly marked ``[paperdb-update]`` line appended.
    """
    payload_inner = json.dumps({NOTICE_KEY: notice}, ensure_ascii=False)[1:-1]
    stripped = text.rstrip()
    if stripped.startswith("{") and stripped.endswith("}"):
        inner = stripped[1:-1].strip()
        sep = ", " if inner else ""
        return f"{stripped[:-1]}{sep}{payload_inner}}}"
    return f"{text}\n{MARKER} {{{payload_inner}}}"


def wrap_tool(fn: Any) -> Callable[..., Any]:
    """Wrap an MCP tool function so its result can carry the update notice.

    ``functools.wraps`` keeps the name, docstring and signature (``inspect``
    follows ``__wrapped__``), so the generated tool schema is unchanged.  The
    wrapper works for sync and async tools and leaves non-string results
    (images) untouched — the notice then rides the next text result.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, str):
            return result
        notice = pending_notice()
        if notice is None:
            return result
        return _inject(result, notice)

    return wrapper


def reset() -> None:
    """Forget in-memory state (tests); the on-disk cache is untouched."""
    global _task, _installed
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
    _notified.clear()
    _installed = _UNSET
