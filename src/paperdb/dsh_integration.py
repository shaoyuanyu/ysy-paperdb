"""Ship and install the DeepSeek Harness integration (MCP bundle + skill).

The DSH integration lives outside the Python package: a configuration-only
bundle (``cordis.patch.yml``) and the ``my-paperdb`` skill.  Both are published
as ONE GitHub Release asset so a running DSH session can update them without
touching this checkout:

    GET https://api.github.com/repos/<repo>/releases/latest
    GET <browser_download_url>      paperdb-dsh-<version>.tar.gz
    GET <SHA256SUMS browser_download_url>          (fallback integrity check)

Asset layout::

    manifest.json
    bundle/package.json
    bundle/cordis.patch.yml          # template, rendered for this machine
    skill/my-paperdb/SKILL.md

The MCP server exposes ``dsh_integration_status`` / ``update_dsh_integration``
/ ``rollback_dsh_integration``.  Applying an update writes files on the machine
running the MCP server, so it is gated behind the owner's current TOTP.

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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_REPO = "shaoyuanyu/ysy-paperdb"
INTEGRATION_DIR_ENV = "PAPERDB_DSH_INTEGRATION_DIR"
REPO_ENV = "PAPERDB_UPDATE_REPO"
GITHUB_TOKEN_ENV = "PAPERDB_GITHUB_TOKEN"

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
MANIFEST_KIND = "paperdb-dsh-integration"
STATE_DIRNAME = ".paperdb-update"
MAX_TARBALL_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
KEEP_BACKUPS = 3

ASSET_RE = re.compile(r"^paperdb-dsh-(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\.tar\.gz$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")

# Asset path -> path relative to the DSH integration directory.
INSTALL_MAP: dict[str, str] = {
    "bundle/package.json": "mcp/dsh-paperdb-mcp/package.json",
    "bundle/cordis.patch.yml": "mcp/dsh-paperdb-mcp/cordis.patch.yml",
    "skill/my-paperdb/SKILL.md": "skills/my-paperdb/SKILL.md",
}
TEMPLATED = frozenset({"bundle/cordis.patch.yml"})
ALLOWED_MEMBERS = frozenset({MANIFEST_NAME, *INSTALL_MAP})


class IntegrationError(RuntimeError):
    """A release could not be fetched, verified, or installed."""


# --------------------------------------------------------------------------- #
# versions


def parse_version(text: str) -> tuple[int, int, int, int, tuple[str, ...]]:
    """Sortable version key; a release sorts after its own prereleases."""
    match = VERSION_RE.match(text.strip())
    if not match:
        raise IntegrationError(f"not a version: {text!r}")
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
        "User-Agent": "paperdb-mcp",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_token() -> str | None:
    return os.environ.get(GITHUB_TOKEN_ENV) or os.environ.get("GITHUB_TOKEN") or None


def _github_error(response: httpx.Response) -> IntegrationError:
    if response.status_code in (403, 429):
        reset = response.headers.get("X-RateLimit-Reset", "")
        hint = f" (rate limit resets at {reset})" if reset else ""
        return IntegrationError(
            f"GitHub API refused the request{hint}; set PAPERDB_GITHUB_TOKEN to raise the limit"
        )
    if response.status_code == 404:
        return IntegrationError("no published release found for this repository")
    return IntegrationError(
        f"GitHub API returned HTTP {response.status_code}: {response.text[:300]}"
    )


def release_from_payload(payload: Mapping[str, Any]) -> Release:
    tag = str(payload.get("tag_name") or "")
    version = version_text(tag)
    if not VERSION_RE.match(version):
        raise IntegrationError(f"release tag is not a version: {tag!r}")
    assets = payload.get("assets") or []
    asset = None
    checksums_url = None
    for item in assets:
        name = str(item.get("name") or "")
        if name == f"paperdb-dsh-{version}.tar.gz":
            asset = item
        elif name == "SHA256SUMS":
            checksums_url = item.get("browser_download_url")
    if asset is None:
        for item in assets:
            name = str(item.get("name") or "")
            match = ASSET_RE.match(name)
            if match and match.group("version") == version:
                asset = item
                break
    if asset is None:
        raise IntegrationError(
            f"release {tag} has no paperdb-dsh-{version}.tar.gz asset "
            "(the release workflow must publish the integration tarball)"
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


async def fetch_release(
    client: httpx.AsyncClient,
    repo: str = DEFAULT_REPO,
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
        raise IntegrationError("GitHub API returned a non-JSON response") from exc
    return release_from_payload(payload)


async def download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    token: str | None = None,
    max_bytes: int = MAX_TARBALL_BYTES,
) -> bytes:
    headers = _headers(token) if "api.github.com" in url else {"User-Agent": "paperdb-mcp"}
    async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
        if response.status_code >= 400:
            raise IntegrationError(f"download failed with HTTP {response.status_code}: {url}")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise IntegrationError(f"release asset is larger than {max_bytes} bytes")
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
    except IntegrationError:
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
        raise IntegrationError(f"release asset contains an unsafe path: {name!r}")
    return "/".join(parts)


def verify_asset(
    data: bytes,
    *,
    expected_sha256: str | None = None,
    expected_version: str | None = None,
) -> VerifiedAsset:
    """Check the tarball digest, then every extracted file against the manifest."""
    if expected_sha256 and len(data) > 0 and sha256_hex(data) != expected_sha256.lower():
        raise IntegrationError(
            "release asset digest mismatch: "
            f"expected {expected_sha256[:12]}…, got {sha256_hex(data)[:12]}…"
        )
    if len(data) > MAX_TARBALL_BYTES:
        raise IntegrationError(f"release asset is larger than {MAX_TARBALL_BYTES} bytes")
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    raise IntegrationError(
                        f"release asset contains a non-file entry: {member.name!r}"
                    )
                name = _safe_member_name(member.name)
                if name not in ALLOWED_MEMBERS:
                    raise IntegrationError(f"release asset contains an unexpected file: {name!r}")
                if name in files:
                    raise IntegrationError(f"release asset repeats {name!r}")
                if member.size > MAX_FILE_BYTES:
                    raise IntegrationError(f"{name!r} is larger than {MAX_FILE_BYTES} bytes")
                handle = archive.extractfile(member)
                if handle is None:  # pragma: no cover - isfile() implies a handle
                    raise IntegrationError(f"cannot read {name!r} from the release asset")
                files[name] = handle.read()
    except tarfile.TarError as exc:
        raise IntegrationError("release asset is not a valid .tar.gz archive") from exc
    missing = ALLOWED_MEMBERS - set(files)
    if missing:
        raise IntegrationError(f"release asset is missing: {', '.join(sorted(missing))}")
    try:
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != MANIFEST_SCHEMA:
        raise IntegrationError("manifest.json has an unsupported schemaVersion")
    if manifest.get("kind") != MANIFEST_KIND:
        raise IntegrationError("manifest.json does not describe a DSH integration")
    version = str(manifest.get("version") or "")
    if expected_version and version != expected_version:
        raise IntegrationError(
            f"manifest version {version!r} does not match the release version {expected_version!r}"
        )
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entries[str(entry["path"])] = entry
    if set(entries) != {name for name in ALLOWED_MEMBERS if name != MANIFEST_NAME}:
        raise IntegrationError("manifest.json does not list exactly the shipped integration files")
    for name, entry in entries.items():
        body = files[name]
        try:
            size = int(entry.get("size", -1))
        except (TypeError, ValueError):
            size = -1
        if size != len(body) or str(entry.get("sha256")) != sha256_hex(body):
            raise IntegrationError(f"manifest hash mismatch for {name!r}")
    return VerifiedAsset(manifest=manifest, files=files)


# --------------------------------------------------------------------------- #
# writing the integration


def resolve_target(value: str | None) -> Path:
    if not value:
        raise IntegrationError(
            f"{INTEGRATION_DIR_ENV} is not set; point it at the directory that holds "
            "<dir>/mcp/dsh-paperdb-mcp and <dir>/skills/my-paperdb"
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise IntegrationError(f"{INTEGRATION_DIR_ENV} must be an absolute path")
    resolved = path.resolve()
    if resolved == Path("/"):
        raise IntegrationError(f"{INTEGRATION_DIR_ENV} cannot be the filesystem root")
    return resolved


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


def render_bundle(template: str, context: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise IntegrationError(f"bundle template uses an unknown placeholder: {key!r}")
        # A JSON string is a valid YAML double-quoted scalar: safe for paths,
        # URLs and tokens regardless of punctuation.
        return json.dumps(context[key])

    rendered = PLACEHOLDER_RE.sub(replace, template)
    if "{{" in rendered:
        raise IntegrationError("bundle template has an unrendered placeholder")
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


def _state_path(target: Path) -> Path:
    return target / STATE_DIRNAME / "state.json"


def read_state(target: Path) -> dict[str, Any]:
    path = _state_path(target)
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(target: Path, state: Mapping[str, Any]) -> None:
    _atomic_write(
        _state_path(target),
        (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _destination(target: Path, rel: str) -> Path:
    """Guard against a symlinked parent redirecting an install outside target."""
    destination = target / rel
    if not destination.resolve().is_relative_to(target):
        raise IntegrationError(f"{rel} resolves outside {target}")
    return destination


def _installed_files(target: Path) -> dict[str, bytes | None]:
    return {
        rel: (target / rel).read_bytes() if (target / rel).is_file() else None
        for rel in INSTALL_MAP.values()
    }


def installed_status(target: Path) -> dict[str, Any]:
    """Version/state snapshot plus a drift check against the last install."""
    state = read_state(target)
    bundle_manifest: dict[str, Any] = {}
    bundle_path = target / "mcp/dsh-paperdb-mcp/package.json"
    if bundle_path.is_file():
        try:
            loaded = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle_manifest = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            bundle_manifest = {}
    raw_recorded = state.get("files")
    recorded: dict[str, Any] = raw_recorded if isinstance(raw_recorded, dict) else {}
    files = []
    for rel, data in _installed_files(target).items():
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
        "target": str(target),
        # Only a release-tracked install has a comparable version; the version
        # in package.json may predate release tracking (or be hand-edited).
        "version": state.get("version"),
        "bundleVersion": bundle_manifest.get("version"),
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


def _backup_dir(target: Path, version: str | None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = re.sub(r"[^0-9A-Za-z._-]+", "-", version or "untracked")[:40]
    return target / STATE_DIRNAME / "backups" / f"{stamp}-{label}"


def _prune_backups(target: Path) -> None:
    root = target / STATE_DIRNAME / "backups"
    if not root.is_dir():
        return
    backups = sorted(item for item in root.iterdir() if item.is_dir())
    for stale in backups[:-KEEP_BACKUPS]:
        shutil.rmtree(stale, ignore_errors=True)


def install(
    target: Path,
    asset: VerifiedAsset,
    release: Release,
    *,
    context: Mapping[str, str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write the verified integration under ``target``, keeping one backup."""
    target = target.resolve()
    previous_state = read_state(target)
    before = previous_state.get("version")
    writes: dict[str, bytes] = {}
    for name in INSTALL_MAP:
        body = asset.files[name]
        if name in TEMPLATED:
            body = render_bundle(body.decode("utf-8"), context).encode("utf-8")
        writes[INSTALL_MAP[name]] = body

    actions = []
    for rel, body in writes.items():
        existing = _destination(target, rel)
        old = existing.read_bytes() if existing.is_file() else None
        action = "create" if old is None else ("unchanged" if old == body else "update")
        actions.append({"path": rel, "action": action, "bytes": len(body)})
    bundle_changed = any(
        item["action"] != "unchanged"
        for item in actions
        if item["path"].startswith("mcp/dsh-paperdb-mcp/")
    )
    report: dict[str, Any] = {
        "ok": True,
        "dryRun": dry_run,
        "from": before,
        "to": asset.version,
        "release": release.tag,
        "assets": actions,
        "restartRequired": bundle_changed,
    }
    if dry_run:
        report["wouldChange"] = sorted(
            item["path"] for item in actions if item["action"] != "unchanged"
        )
        return report

    backup: Path | None = None
    existed = {rel: _destination(target, rel).is_file() for rel in writes}
    if any(existed.values()):
        backup = _backup_dir(target, before)
        for rel, present in existed.items():
            if present:
                destination = backup / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(_destination(target, rel), destination)
        _atomic_write(
            backup / "meta.json",
            json.dumps(
                {
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

    state = {
        "schemaVersion": 1,
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
    _write_state(target, state)
    _prune_backups(target)
    report["backup"] = str(backup) if backup is not None else None
    report["changed"] = sorted(item["path"] for item in actions if item["action"] != "unchanged")
    return report


def release_url_repo(page_url: str) -> str:
    """``https://github.com/owner/name/releases/tag/v1`` -> ``owner/name``."""
    match = re.match(r"^https?://github\.com/([^/]+/[^/]+)/releases/", page_url)
    return match.group(1) if match else ""


def rollback(target: Path) -> dict[str, Any]:
    target = target.resolve()
    state = read_state(target)
    previous = state.get("previous")
    if not isinstance(previous, dict) or not previous.get("backup"):
        raise IntegrationError("no previous integration version to roll back to")
    backup = (target / str(previous["backup"])).resolve()
    if not backup.is_relative_to(target):
        raise IntegrationError("backup path escapes the integration directory")
    meta_path = backup / "meta.json"
    if not meta_path.is_file():
        raise IntegrationError(f"backup is incomplete: {backup}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"backup metadata is unreadable: {backup}") from exc
    existed = meta.get("existed") if isinstance(meta.get("existed"), dict) else {}
    known = set(INSTALL_MAP.values())
    if set(existed) - known:
        raise IntegrationError("backup references paths outside the integration layout")
    restored: list[str] = []
    removed: list[str] = []
    for rel, present in existed.items():
        destination = _destination(target, rel)
        if present:
            source = backup / rel
            if not source.is_file():
                raise IntegrationError(f"backup is missing {rel}")
            _atomic_write(destination, source.read_bytes())
            restored.append(rel)
        elif destination.is_file():
            destination.unlink()
            removed.append(rel)
    files = {
        rel: sha256_hex((target / rel).read_bytes())
        for rel in INSTALL_MAP.values()
        if (target / rel).is_file()
    }
    from_version = previous.get("version")
    state["version"] = from_version
    state["installedAt"] = datetime.now(UTC).isoformat()
    state["files"] = files
    state["previous"] = None
    state["source"] = state.get("source") or {}
    _write_state(target, state)
    return {
        "ok": True,
        "version": from_version,
        "restored": restored,
        "removed": removed,
        "restartRequired": any(path.startswith("mcp/") for path in restored + removed),
    }


# --------------------------------------------------------------------------- #
# build the release asset (used by scripts/build_dsh_release.py and CI)


def build_release(
    repo_root: Path,
    out_dir: Path,
    *,
    version: str,
    tag: str | None = None,
) -> dict[str, Path]:
    source = repo_root / "for-agents"
    bundle_dir = source / "mcp" / "dsh-paperdb-mcp"
    skill_file = source / "skills" / "my-paperdb" / "SKILL.md"
    for required in (bundle_dir / "package.json", bundle_dir / "cordis.patch.yml", skill_file):
        if not required.is_file():
            raise IntegrationError(f"integration source is missing: {required}")
    parse_version(version)
    tag = tag or f"v{version}"

    package = json.loads((bundle_dir / "package.json").read_text(encoding="utf-8"))
    package["version"] = version
    files: dict[str, bytes] = {
        "bundle/package.json": (json.dumps(package, indent=2) + "\n").encode("utf-8"),
        "bundle/cordis.patch.yml": (bundle_dir / "cordis.patch.yml").read_bytes(),
        "skill/my-paperdb/SKILL.md": skill_file.read_bytes(),
    }
    manifest = {
        "schemaVersion": MANIFEST_SCHEMA,
        "kind": MANIFEST_KIND,
        "version": version,
        "tag": tag,
        "bundle": {"name": package.get("name"), "version": version},
        "skill": {"name": "my-paperdb", "version": version},
        "files": [
            {"path": name, "sha256": sha256_hex(body), "size": len(body)}
            for name, body in sorted(files.items())
        ],
    }
    payloads = {MANIFEST_NAME: (json.dumps(manifest, indent=2) + "\n").encode("utf-8"), **files}

    out_dir.mkdir(parents=True, exist_ok=True)
    asset = out_dir / f"paperdb-dsh-{version}.tar.gz"
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
