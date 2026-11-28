"""Host adapters: install this MCP server into a particular agent host.

The MCP server (``paperdb.mcp_server``) is harness-neutral: it speaks stdio MCP,
reads only ``PAPERDB_MCP_*`` configuration, and knows nothing about how a host
registers it.  Everything a *particular* host needs — the file layout it loads,
the template that points it at the ``paperdb-mcp`` launcher, the skill text —
lives behind an :class:`Adapter` and ships as that host's release asset::

    paperdb-adapter-<host>-<version>.tar.gz
        manifest.json
        bundle/package.json          # host bundle manifest (version rendered)
        bundle/cordis.patch.yml      # template, rendered for this machine
        skills/my-paperdb/SKILL.md

Adding a host is a data change — one :class:`Adapter` literal plus a payload
directory under ``adapters/<host>/`` — not a change to the server or its tools.

Trust model: the release asset is verified against the GitHub asset digest
(``sha256:...``), falling back to the release's ``SHA256SUMS`` asset.  Per-file
hashes in ``manifest.json`` then pin the extracted contents.  Everything comes
from one repository over TLS; a compromised GitHub release is out of scope.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_REPO = "shaoyuanyu/ysy-paperdb"
REPO_ENV = "PAPERDB_UPDATE_REPO"
GITHUB_TOKEN_ENV = "PAPERDB_GITHUB_TOKEN"

#: Generic override for the install directory; a host-specific default lives on
#: the adapter, and a pre-rename variable name may still be honoured there.
ADAPTER_DIR_ENV = "PAPERDB_ADAPTER_DIR"
HOST_ENV = "PAPERDB_ADAPTER_HOST"

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
MANIFEST_KIND = "paperdb-adapter"
STATE_SCHEMA = 2
MAX_TARBALL_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
KEEP_BACKUPS = 3

ASSET_RE = re.compile(
    r"^paperdb-adapter-(?P<host>[a-z0-9][a-z0-9-]*)-(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\.tar\.gz$"
)
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")


class AdapterError(RuntimeError):
    """A host adapter could not be fetched, verified, or installed."""


@dataclass(frozen=True)
class Adapter:
    """One agent host's integration layout.

    Attributes:
        host: the host id used on the command line and in the asset name.
        source_dir: payload directory in this repository, relative to the root.
        install_map: asset path -> path relative to the install directory.
        templated: asset paths that contain ``{{placeholder}}`` markers.
        restart_prefixes: installed paths whose change requires a host restart
            (rather than being picked up by the next session).
        state_dirname: where version state and backups live, inside the target.
        skill_name: the skill this adapter installs, for release metadata.
        package_manifest: installed path of the host bundle manifest, read back
            for a "what does the host itself think is installed" cross-check.
        default_target: install directory when no environment override is set.
        dir_env / legacy_dir_env: host-specific directory overrides, tried after
            the generic ``PAPERDB_ADAPTER_DIR``.
        legacy_state_dirname: state directory written by an older release of
            this adapter; read (never written) for upgrade continuity.
    """

    host: str
    source_dir: str
    install_map: Mapping[str, str]
    templated: frozenset[str] = frozenset()
    restart_prefixes: tuple[str, ...] = ("bundle/",)
    state_dirname: str = ".paperdb-adapter"
    skill_name: str = "my-paperdb"
    package_manifest: str | None = None
    default_target: Path | None = None
    dir_env: str | None = None
    legacy_dir_env: str | None = None
    legacy_state_dirname: str | None = None

    @property
    def asset_name_prefix(self) -> str:
        return f"paperdb-adapter-{self.host}"

    def asset_name(self, version: str) -> str:
        return f"{self.asset_name_prefix}-{version}.tar.gz"

    def allowed_members(self) -> frozenset[str]:
        return frozenset({MANIFEST_NAME, *self.install_map})

    def restart_required(self, paths: Sequence[str]) -> bool:
        return any(path.startswith(self.restart_prefixes) for path in paths)


# --------------------------------------------------------------------------- #
# versions


def parse_version(text: str) -> tuple[int, int, int, int, tuple[str, ...]]:
    """Sortable version key; a release sorts after its own prereleases."""
    match = VERSION_RE.match(text.strip())
    if not match:
        raise AdapterError(f"not a version: {text!r}")
    major, minor, patch, prerelease = match.groups()
    parts = tuple(part for part in (prerelease or "").split(".") if part)
    return (int(major), int(minor), int(patch), 0 if parts else 1, parts)


def version_text(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


# --------------------------------------------------------------------------- #
# GitHub release metadata


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    published_at: str
    prerelease: bool
    notes: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_sha256: str | None
    checksums_url: str | None


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "paperdb-agent-adapter",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_token() -> str | None:
    return os.environ.get(GITHUB_TOKEN_ENV) or os.environ.get("GITHUB_TOKEN") or None


def _github_error(response: httpx.Response) -> AdapterError:
    if response.status_code in (403, 429):
        reset = response.headers.get("X-RateLimit-Reset", "")
        hint = f" (rate limit resets at {reset})" if reset else ""
        return AdapterError(
            f"GitHub API refused the request{hint}; set PAPERDB_GITHUB_TOKEN to raise the limit"
        )
    if response.status_code == 404:
        return AdapterError("no published release found for this repository")
    return AdapterError(f"GitHub API returned HTTP {response.status_code}: {response.text[:300]}")


def release_from_payload(payload: Mapping[str, Any], adapter: Adapter) -> Release:
    tag = str(payload.get("tag_name") or "")
    version = version_text(tag)
    if not VERSION_RE.match(version):
        raise AdapterError(f"release tag is not a version: {tag!r}")
    assets = payload.get("assets") or []
    asset = None
    checksums_url = None
    expected = adapter.asset_name(version)
    for item in assets:
        name = str(item.get("name") or "")
        if name == expected:
            asset = item
        elif name == "SHA256SUMS":
            checksums_url = item.get("browser_download_url")
    if asset is None:
        for item in assets:
            name = str(item.get("name") or "")
            match = ASSET_RE.match(name)
            if match and match.group("host") == adapter.host and match.group("version") == version:
                asset = item
                break
    if asset is None:
        raise AdapterError(
            f"release {tag} has no {expected} asset "
            "(the release workflow must publish the adapter tarball)"
        )
    digest = str(asset.get("digest") or "")
    sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None
    body = str(payload.get("body") or "")
    return Release(
        version=version,
        tag=tag,
        published_at=str(payload.get("published_at") or ""),
        prerelease=bool(payload.get("prerelease")),
        notes=body[:4000],
        page_url=str(payload.get("html_url") or ""),
        asset_name=str(asset.get("name") or ""),
        asset_url=str(asset.get("browser_download_url") or ""),
        asset_sha256=sha,
        checksums_url=str(checksums_url) if checksums_url else None,
    )


def repo_name() -> str:
    return os.environ.get(REPO_ENV) or DEFAULT_REPO


async def fetch_release(
    client: httpx.AsyncClient,
    repo: str,
    adapter: Adapter,
    *,
    version: str | None = None,
    token: str | None = None,
) -> Release:
    """Latest stable release, or a pinned one (``version`` without the ``v``)."""
    if version:
        url = f"https://api.github.com/repos/{repo}/releases/tags/v{version}"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
    response = await client.get(url, headers=_headers(token))
    if response.status_code >= 400:
        raise _github_error(response)
    try:
        payload = response.json()
    except ValueError as exc:  # pragma: no cover - GitHub always returns JSON
        raise AdapterError("GitHub API returned a non-JSON response") from exc
    return release_from_payload(payload, adapter)


async def download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    token: str | None = None,
    max_bytes: int = MAX_TARBALL_BYTES,
) -> bytes:
    headers = (
        _headers(token) if "api.github.com" in url else {"User-Agent": "paperdb-agent-adapter"}
    )
    async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
        if response.status_code >= 400:
            raise AdapterError(f"download failed with HTTP {response.status_code}: {url}")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise AdapterError(f"release asset is larger than {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


async def checksum_from_release(
    client: httpx.AsyncClient, release: Release, asset_name: str, *, token: str | None = None
) -> str | None:
    if not release.checksums_url:
        return None
    try:
        text = (
            await download_bytes(client, release.checksums_url, token=token, max_bytes=1 << 20)
        ).decode()
    except AdapterError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset_name:
            return parts[0].lower()
    return None


# --------------------------------------------------------------------------- #
# asset verification


@dataclass(frozen=True)
class VerifiedAsset:
    manifest: dict[str, Any]
    files: dict[str, bytes]  # asset path -> shipped bytes (templates not rendered)

    @property
    def version(self) -> str:
        return str(self.manifest["version"])


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_member_name(name: str) -> str:
    cleaned = name.replace("\\", "/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or cleaned.startswith("/"):
        raise AdapterError(f"release asset contains an unsafe path: {name!r}")
    return "/".join(parts)


def verify_asset(
    adapter: Adapter,
    data: bytes,
    *,
    expected_sha256: str | None = None,
    expected_version: str | None = None,
) -> VerifiedAsset:
    """Check the tarball digest, then every extracted file against the manifest."""
    allowed = adapter.allowed_members()
    if expected_sha256 and len(data) > 0 and sha256_hex(data) != expected_sha256.lower():
        raise AdapterError(
            "release asset digest mismatch: "
            f"expected {expected_sha256[:12]}…, got {sha256_hex(data)[:12]}…"
        )
    if len(data) > MAX_TARBALL_BYTES:
        raise AdapterError(f"release asset is larger than {MAX_TARBALL_BYTES} bytes")
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    raise AdapterError(f"release asset contains a non-file entry: {member.name!r}")
                name = _safe_member_name(member.name)
                if name not in allowed:
                    raise AdapterError(f"release asset contains an unexpected file: {name!r}")
                if name in files:
                    raise AdapterError(f"release asset repeats {name!r}")
                if member.size > MAX_FILE_BYTES:
                    raise AdapterError(f"{name!r} is larger than {MAX_FILE_BYTES} bytes")
                handle = archive.extractfile(member)
                if handle is None:  # pragma: no cover - isfile() implies a handle
                    raise AdapterError(f"cannot read {name!r} from the release asset")
                files[name] = handle.read()
    except tarfile.TarError as exc:
        raise AdapterError("release asset is not a valid .tar.gz archive") from exc
    missing = allowed - set(files)
    if missing:
        raise AdapterError(f"release asset is missing: {', '.join(sorted(missing))}")
    try:
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != MANIFEST_SCHEMA:
        raise AdapterError("manifest.json has an unsupported schemaVersion")
    if manifest.get("kind") != MANIFEST_KIND:
        raise AdapterError("manifest.json does not describe a paperdb host adapter")
    if manifest.get("host") != adapter.host:
        raise AdapterError(
            f"manifest.json is for host {manifest.get('host')!r}, not {adapter.host!r}"
        )
    version = str(manifest.get("version") or "")
    if expected_version and version != expected_version:
        raise AdapterError(
            f"manifest version {version!r} does not match the release version {expected_version!r}"
        )
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entries[str(entry["path"])] = entry
    if set(entries) != {name for name in allowed if name != MANIFEST_NAME}:
        raise AdapterError("manifest.json does not list exactly the shipped adapter files")
    for name, entry in entries.items():
        body = files[name]
        try:
            size = int(entry.get("size", -1))
        except (TypeError, ValueError):
            size = -1
        if size != len(body) or str(entry.get("sha256")) != sha256_hex(body):
            raise AdapterError(f"manifest hash mismatch for {name!r}")
    return VerifiedAsset(manifest=manifest, files=files)


# --------------------------------------------------------------------------- #
# resolving and writing the install


def resolve_target(adapter: Adapter, value: str | None = None) -> Path:
    """Explicit path, then generic env, then host env, then the host default."""
    candidates: list[tuple[str, str]] = [("argument", item) for item in [value] if item]
    for env in (ADAPTER_DIR_ENV, adapter.dir_env, adapter.legacy_dir_env):
        if env and os.environ.get(env):
            candidates.append((env, os.environ[env]))
    if candidate := next(((source, item) for source, item in candidates if item), None):
        source, raw = candidate
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise AdapterError(f"{source} must be an absolute path, got {raw!r}")
        resolved = path.resolve()
        if resolved == Path("/"):
            raise AdapterError(f"{source} cannot be the filesystem root")
        return resolved
    if adapter.default_target is not None:
        resolved = adapter.default_target.expanduser().resolve()
        if resolved != Path("/"):
            return resolved
    names = ", ".join(
        env for env in (ADAPTER_DIR_ENV, adapter.dir_env, adapter.legacy_dir_env) if env
    )
    raise AdapterError(f"no install directory: set {names}")


def default_mcp_command() -> str:
    """The ``paperdb-mcp`` launcher the bundle should spawn on this machine."""
    candidates = [Path(sys.executable).with_name("paperdb-mcp")] if sys.executable else []
    which = shutil.which("paperdb-mcp")
    if which:
        candidates.append(Path(which))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "paperdb-mcp"


def build_context(
    target: Path,
    *,
    base_url: str | None = None,
    repo: str | None = None,
    mcp_command: str | None = None,
) -> dict[str, str]:
    """Template values for a host bundle.

    Deliberately holds no secret: the server reads its token from the token
    file, so a rendered bundle stays safe to publish and to diff.
    """
    from paperdb import mcp_config

    return {
        "mcp_command": mcp_command or default_mcp_command(),
        "base_url": (base_url or mcp_config.base_url()).rstrip("/"),
        "target": str(target),
        "repo": repo or repo_name(),
    }


def render_bundle(template: str, context: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise AdapterError(f"bundle template uses an unknown placeholder: {key!r}")
        # A JSON string is a valid YAML double-quoted scalar: safe for paths,
        # URLs and tokens regardless of punctuation.
        return json.dumps(context[key])

    rendered = PLACEHOLDER_RE.sub(replace, template)
    if "{{" in rendered:
        raise AdapterError("bundle template has an unrendered placeholder")
    return rendered


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _state_dirs(adapter: Adapter) -> list[str]:
    dirs = [adapter.state_dirname]
    if adapter.legacy_state_dirname:
        dirs.append(adapter.legacy_state_dirname)
    return dirs


def _state_path(target: Path, dirname: str) -> Path:
    return target / dirname / "state.json"


def read_state(adapter: Adapter, target: Path) -> dict[str, Any]:
    """State written by this adapter version, else by its pre-rename version."""
    for dirname in _state_dirs(adapter):
        path = _state_path(target, dirname)
        if not path.is_file():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(state, dict):
            if state.get("host") not in (None, adapter.host):
                continue
            return state
    return {}


def _write_state(adapter: Adapter, target: Path, state: Mapping[str, Any]) -> None:
    _atomic_write(
        _state_path(target, adapter.state_dirname),
        (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _destination(target: Path, rel: str) -> Path:
    """Guard against a symlinked parent redirecting an install outside target."""
    destination = target / rel
    if not destination.resolve().is_relative_to(target):
        raise AdapterError(f"{rel} resolves outside {target}")
    return destination


def installed_status(adapter: Adapter, target: Path) -> dict[str, Any]:
    """Version/state snapshot plus a drift check against the last install."""
    state = read_state(adapter, target)
    package_manifest: dict[str, Any] = {}
    if adapter.package_manifest:
        package_path = target / adapter.package_manifest
        if package_path.is_file():
            try:
                loaded = json.loads(package_path.read_text(encoding="utf-8"))
                package_manifest = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                package_manifest = {}
    raw_recorded = state.get("files")
    recorded: dict[str, Any] = raw_recorded if isinstance(raw_recorded, dict) else {}
    files = []
    for rel in adapter.install_map.values():
        path = target / rel
        data = path.read_bytes() if path.is_file() else None
        current = sha256_hex(data) if data is not None else None
        files.append(
            {
                "path": rel,
                "installed": data is not None,
                "sha256": current,
                "matchesState": current is not None and recorded.get(rel) == current,
            }
        )
    return {
        "host": adapter.host,
        "target": str(target),
        # Only a release-tracked install has a comparable version; the version
        # in the host package manifest may predate release tracking.
        "version": state.get("version"),
        "packageVersion": package_manifest.get("version"),
        "installedAt": state.get("installedAt"),
        "source": state.get("source"),
        "previous": state.get("previous"),
        "tracked": bool(state),
        "files": files,
        "drifted": sorted(
            item["path"]
            for item in files
            if state and item["installed"] and not item["matchesState"]
        ),
    }


def _backup_dir(adapter: Adapter, target: Path, version: str | None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = re.sub(r"[^0-9A-Za-z._-]+", "-", version or "untracked")[:40]
    return target / adapter.state_dirname / "backups" / f"{stamp}-{label}"


def _prune_backups(adapter: Adapter, target: Path) -> None:
    root = target / adapter.state_dirname / "backups"
    if not root.is_dir():
        return
    backups = sorted(item for item in root.iterdir() if item.is_dir())
    for stale in backups[:-KEEP_BACKUPS]:
        shutil.rmtree(stale, ignore_errors=True)


def install(
    adapter: Adapter,
    target: Path,
    asset: VerifiedAsset,
    release: Release,
    *,
    context: Mapping[str, str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write the verified adapter under ``target``, keeping one backup."""
    target = target.resolve()
    previous_state = read_state(adapter, target)
    before = previous_state.get("version")
    writes: dict[str, bytes] = {}
    for asset_path in adapter.install_map:
        body = asset.files[asset_path]
        if asset_path in adapter.templated:
            body = render_bundle(body.decode("utf-8"), context).encode("utf-8")
        writes[adapter.install_map[asset_path]] = body

    actions = []
    for rel, body in writes.items():
        existing = _destination(target, rel)
        old = existing.read_bytes() if existing.is_file() else None
        action = "create" if old is None else ("unchanged" if old == body else "update")
        actions.append({"path": rel, "action": action, "bytes": len(body)})
    changed = sorted(item["path"] for item in actions if item["action"] != "unchanged")
    report: dict[str, Any] = {
        "ok": True,
        "host": adapter.host,
        "dryRun": dry_run,
        "from": before,
        "to": asset.version,
        "release": release.tag,
        "target": str(target),
        "assets": actions,
        "restartRequired": adapter.restart_required(changed),
    }
    if dry_run:
        report["wouldChange"] = changed
        return report

    backup: Path | None = None
    existed = {rel: _destination(target, rel).is_file() for rel in writes}
    if any(existed.values()):
        backup = _backup_dir(adapter, target, before)
        for rel, present in existed.items():
            if present:
                destination = backup / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(_destination(target, rel), destination)
        _atomic_write(
            backup / "meta.json",
            json.dumps(
                {
                    "host": adapter.host,
                    "fromVersion": before,
                    "toVersion": asset.version,
                    "existed": existed,
                    "createdAt": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ).encode("utf-8"),
        )

    for rel, body in writes.items():
        _atomic_write(_destination(target, rel), body)

    state: dict[str, Any] = {
        "schemaVersion": STATE_SCHEMA,
        "host": adapter.host,
        "version": asset.version,
        "installedAt": datetime.now(UTC).isoformat(),
        "source": {
            "repo": release_url_repo(release.page_url),
            "tag": release.tag,
            "page": release.page_url,
            "asset": release.asset_name,
            "assetSha256": release.asset_sha256,
        },
        "files": {rel: sha256_hex(body) for rel, body in writes.items()},
        "previous": None,
    }
    if backup is not None:
        state["previous"] = {"version": before, "backup": str(backup.relative_to(target))}
    _write_state(adapter, target, state)
    _prune_backups(adapter, target)
    report["backup"] = str(backup) if backup is not None else None
    report["changed"] = changed
    return report


def release_url_repo(page_url: str) -> str:
    """``https://github.com/owner/name/releases/tag/v1`` -> ``owner/name``."""
    match = re.match(r"^https?://github\.com/([^/]+/[^/]+)/releases/", page_url)
    return match.group(1) if match else ""


def rollback(adapter: Adapter, target: Path) -> dict[str, Any]:
    target = target.resolve()
    state = read_state(adapter, target)
    previous = state.get("previous")
    if not isinstance(previous, dict) or not previous.get("backup"):
        raise AdapterError("no previous adapter version to roll back to")
    backup = (target / str(previous["backup"])).resolve()
    if not backup.is_relative_to(target):
        raise AdapterError("backup path escapes the install directory")
    meta_path = backup / "meta.json"
    if not meta_path.is_file():
        raise AdapterError(f"backup is incomplete: {backup}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"backup metadata is unreadable: {backup}") from exc
    existed = meta.get("existed") if isinstance(meta.get("existed"), dict) else {}
    known = set(adapter.install_map.values())
    if set(existed) - known:
        raise AdapterError("backup references paths outside the adapter layout")
    restored: list[str] = []
    removed: list[str] = []
    for rel, present in existed.items():
        destination = _destination(target, rel)
        if present:
            source = backup / rel
            if not source.is_file():
                raise AdapterError(f"backup is missing {rel}")
            _atomic_write(destination, source.read_bytes())
            restored.append(rel)
        elif destination.is_file():
            destination.unlink()
            removed.append(rel)
    files = {
        rel: sha256_hex((target / rel).read_bytes())
        for rel in adapter.install_map.values()
        if (target / rel).is_file()
    }
    from_version = previous.get("version")
    state["host"] = adapter.host
    state["schemaVersion"] = STATE_SCHEMA
    state["version"] = from_version
    state["installedAt"] = datetime.now(UTC).isoformat()
    state["files"] = files
    state["previous"] = None
    state["source"] = state.get("source") or {}
    _write_state(adapter, target, state)
    return {
        "ok": True,
        "host": adapter.host,
        "version": from_version,
        "target": str(target),
        "restored": restored,
        "removed": removed,
        "restartRequired": adapter.restart_required(restored + removed),
    }


# --------------------------------------------------------------------------- #
# build the release asset (used by scripts/build_adapter_release.py and CI)


def build_asset(
    adapter: Adapter,
    repo_root: Path,
    out_dir: Path,
    *,
    version: str,
    tag: str | None = None,
) -> dict[str, Path]:
    """Pack ``adapters/<host>/`` into the release asset for that host."""
    parse_version(version)
    tag = tag or f"v{version}"
    source = repo_root / adapter.source_dir
    if not source.is_dir():
        raise AdapterError(f"adapter source is missing: {source}")
    files: dict[str, bytes] = {}
    for asset_path in adapter.install_map:
        path = source / asset_path
        if not path.is_file():
            raise AdapterError(f"adapter source is missing: {path}")
        body = path.read_bytes()
        if asset_path == "bundle/package.json":
            package = json.loads(body.decode("utf-8"))
            package["version"] = version
            body = (json.dumps(package, indent=2) + "\n").encode("utf-8")
        files[asset_path] = body
    manifest = {
        "schemaVersion": MANIFEST_SCHEMA,
        "kind": MANIFEST_KIND,
        "host": adapter.host,
        "version": version,
        "tag": tag,
        "skill": {"name": adapter.skill_name, "version": version},
        "files": [
            {"path": name, "sha256": sha256_hex(body), "size": len(body)}
            for name, body in sorted(files.items())
        ],
    }
    payloads = {MANIFEST_NAME: (json.dumps(manifest, indent=2) + "\n").encode("utf-8"), **files}

    out_dir.mkdir(parents=True, exist_ok=True)
    asset = out_dir / adapter.asset_name(version)
    asset.write_bytes(_tar_gz(payloads))
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_bytes(payloads[MANIFEST_NAME])
    return {"asset": asset, "manifest": manifest_path}


def _tar_gz(payloads: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in sorted(payloads):
            body = payloads[name]
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(body))
    return gzip.compress(buffer.getvalue(), mtime=0)
