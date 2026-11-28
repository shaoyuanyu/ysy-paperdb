"""DSH integration packaging: build, verify, install, roll back.

These tests exercise the real archive formats — the same bytes the release
workflow publishes and `update_dsh_integration` installs — so a manifest or
path-mapping change fails here before it reaches a DSH profile.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from paperdb import dsh_integration as dsh

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tar_gz(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(body))
    return gzip.compress(buffer.getvalue(), mtime=0)


def _release(version: str, sha: str | None) -> dsh.Release:
    return dsh.Release(
        version=version,
        tag=f"v{version}",
        published_at="2026-11-28T00:00:00Z",
        prerelease=False,
        notes=f"notes for {version}",
        page_url=f"https://github.com/shaoyuanyu/ysy-paperdb/releases/tag/v{version}",
        asset_name=f"paperdb-dsh-{version}.tar.gz",
        asset_url=f"https://example.test/paperdb-dsh-{version}.tar.gz",
        asset_sha256=sha,
        checksums_url="https://example.test/SHA256SUMS",
    )


def _built(tmp_path: Path, version: str) -> tuple[bytes, str]:
    paths = dsh.build_release(REPO_ROOT, tmp_path / "dist", version=version)
    data = paths["asset"].read_bytes()
    return data, dsh.sha256_hex(data)


def _context(target: Path) -> dict[str, str]:
    return {
        "mcp_command": "/opt/venv/bin/paperdb-mcp",
        "base_url": "https://library.example.test",
        "token": 'pdb_pat_"quoted"',
        "target": str(target),
        "repo": "shaoyuanyu/ysy-paperdb",
    }


def _verified(tmp_path: Path, version: str) -> dsh.VerifiedAsset:
    data, sha = _built(tmp_path, version)
    return dsh.verify_asset(data, expected_sha256=sha, expected_version=version)


def test_build_install_rollback_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "for-agents"
    asset = _verified(tmp_path, "0.2.0")
    data, sha = _built(tmp_path, "0.2.0")

    report = dsh.install(target, asset, _release("0.2.0", sha), context=_context(target))
    assert report["ok"] and report["to"] == "0.2.0"
    assert report["restartRequired"] is True
    assert (target / "mcp/dsh-paperdb-mcp/package.json").is_file()
    rendered = (target / "mcp/dsh-paperdb-mcp/cordis.patch.yml").read_text(encoding="utf-8")
    assert "{{" not in rendered
    assert f'command: "{_context(target)["mcp_command"]}"' in rendered
    assert str(target) in rendered
    # The bundle must stay secret-free: the token is read from a file at
    # runtime, so a published release asset never carries a credential.
    assert _context(target)["token"] not in rendered
    assert "PAPERDB_MCP_TOKEN_FILE" in rendered
    assert (target / "skills/my-paperdb/SKILL.md").read_text(encoding="utf-8").startswith("---")

    status = dsh.installed_status(target)
    assert status["version"] == "0.2.0"
    assert status["tracked"] is True
    assert status["drifted"] == []
    assert {item["path"] for item in status["files"]} == set(dsh.INSTALL_MAP.values())

    first_skill = (target / "skills/my-paperdb/SKILL.md").read_bytes()
    assert data  # tarball itself is what install verified

    newer = _verified(tmp_path, "0.3.0")
    _, newer_sha = _built(tmp_path, "0.3.0")
    second = dsh.install(target, newer, _release("0.3.0", newer_sha), context=_context(target))
    assert second["to"] == "0.3.0"
    assert second["backup"] and Path(second["backup"]).is_dir()
    assert (target / ".paperdb-update/backups").is_dir()

    restored = dsh.rollback(target)
    assert restored["ok"] and restored["version"] == "0.2.0"
    assert restored["restartRequired"] is True
    assert (target / "skills/my-paperdb/SKILL.md").read_bytes() == first_skill
    package = json.loads((target / "mcp/dsh-paperdb-mcp/package.json").read_text(encoding="utf-8"))
    assert package["version"] == "0.2.0"
    assert dsh.installed_status(target)["version"] == "0.2.0"


def test_rollback_without_backup_fails(tmp_path: Path) -> None:
    with pytest.raises(dsh.IntegrationError, match="no previous"):
        dsh.rollback(tmp_path / "empty")


def test_install_detects_local_drift(tmp_path: Path) -> None:
    target = tmp_path / "drift"
    asset = _verified(tmp_path, "0.2.0")
    _, sha = _built(tmp_path, "0.2.0")
    dsh.install(target, asset, _release("0.2.0", sha), context=_context(target))
    skill = target / "skills/my-paperdb/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    assert dsh.installed_status(target)["drifted"] == ["skills/my-paperdb/SKILL.md"]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "dry"
    asset = _verified(tmp_path, "0.2.0")
    _, sha = _built(tmp_path, "0.2.0")
    report = dsh.install(
        target, asset, _release("0.2.0", sha), context=_context(target), dry_run=True
    )
    assert report["dryRun"] is True
    assert report["wouldChange"] == sorted(dsh.INSTALL_MAP.values())
    assert not target.exists()


def test_asset_rejects_path_traversal(tmp_path: Path) -> None:
    data = _tar_gz({"../escape.txt": b"owned"})
    with pytest.raises(dsh.IntegrationError, match="unsafe path"):
        dsh.verify_asset(data)


def test_asset_rejects_unexpected_files() -> None:
    data = _tar_gz({"notes.txt": b"hello"})
    with pytest.raises(dsh.IntegrationError, match="unexpected file"):
        dsh.verify_asset(data)


def test_asset_rejects_digest_mismatch(tmp_path: Path) -> None:
    data, _ = _built(tmp_path, "0.2.0")
    with pytest.raises(dsh.IntegrationError, match="digest mismatch"):
        dsh.verify_asset(data, expected_sha256="0" * 64)


def test_asset_rejects_manifest_hash_mismatch() -> None:
    files = {
        "bundle/package.json": b"{}",
        "bundle/cordis.patch.yml": b"x",
        "skill/my-paperdb/SKILL.md": b"y",
    }
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": dsh.MANIFEST_KIND,
        "version": "0.2.0",
        "tag": "v0.2.0",
        "files": [
            {"path": name, "sha256": "0" * 64, "size": len(body)} for name, body in files.items()
        ],
    }
    data = _tar_gz({dsh.MANIFEST_NAME: json.dumps(manifest).encode(), **files})
    with pytest.raises(dsh.IntegrationError, match="hash mismatch"):
        dsh.verify_asset(data)


def test_asset_requires_manifest_version_to_match(tmp_path: Path) -> None:
    data, sha = _built(tmp_path, "0.2.0")
    with pytest.raises(dsh.IntegrationError, match="does not match the release"):
        dsh.verify_asset(data, expected_sha256=sha, expected_version="0.3.0")


def test_version_ordering() -> None:
    assert dsh.parse_version("0.10.0") > dsh.parse_version("0.9.9")
    assert dsh.parse_version("1.0.0") > dsh.parse_version("1.0.0-rc.1")
    assert dsh.parse_version("v1.2.3") == dsh.parse_version("1.2.3")
    assert dsh.version_text("v1.2.3") == "1.2.3"
    with pytest.raises(dsh.IntegrationError):
        dsh.parse_version("1.2")


def test_render_bundle_quotes_values_and_rejects_unknown(tmp_path: Path) -> None:
    rendered = dsh.render_bundle("a: {{value}}", {"value": 'x"y\\z'})
    assert rendered == 'a: "x\\"y\\\\z"'
    with pytest.raises(dsh.IntegrationError, match="unknown placeholder"):
        dsh.render_bundle("a: {{nope}}", {})


def test_resolve_target_requires_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(dsh.IntegrationError):
        dsh.resolve_target(None)
    with pytest.raises(dsh.IntegrationError):
        dsh.resolve_target("relative/dir")
    assert dsh.resolve_target(str(tmp_path)) == tmp_path.resolve()


def test_release_payload_reads_digest_and_checksums() -> None:
    digest = "ab" * 32
    payload = {
        "tag_name": "v0.2.0",
        "published_at": "2026-11-28T00:00:00Z",
        "prerelease": False,
        "html_url": "https://github.com/shaoyuanyu/ysy-paperdb/releases/tag/v0.2.0",
        "body": "release notes",
        "assets": [
            {
                "name": "paperdb-dsh-0.2.0.tar.gz",
                "browser_download_url": "https://example.test/asset.tar.gz",
                "digest": f"sha256:{digest}",
            },
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://example.test/SHA256SUMS",
            },
        ],
    }
    release = dsh.release_from_payload(payload)
    assert release.version == "0.2.0"
    assert release.asset_sha256 == digest
    assert release.checksums_url == "https://example.test/SHA256SUMS"
    assert release.notes == "release notes"
    assert dsh.release_url_repo(release.page_url) == "shaoyuanyu/ysy-paperdb"


def test_release_payload_without_asset_fails() -> None:
    payload = {"tag_name": "v0.2.0", "assets": [{"name": "paperdb-0.2.0.whl"}]}
    with pytest.raises(dsh.IntegrationError, match="no paperdb-dsh"):
        dsh.release_from_payload(payload)


async def test_fetch_release_hits_latest_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "tag_name": "v0.2.0",
                "published_at": "2026-11-28T00:00:00Z",
                "html_url": "https://github.com/shaoyuanyu/ysy-paperdb/releases/tag/v0.2.0",
                "assets": [
                    {
                        "name": "paperdb-dsh-0.2.0.tar.gz",
                        "browser_download_url": "https://example.test/a.tar.gz",
                        "digest": f"sha256:{'ab' * 32}",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        release = await dsh.fetch_release(client, "shaoyuanyu/ysy-paperdb")
    assert seen == ["/repos/shaoyuanyu/ysy-paperdb/releases/latest"]
    assert release.version == "0.2.0"
    assert release.asset_sha256 == "ab" * 32


async def test_fetch_release_reports_missing_release() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(dsh.IntegrationError, match="no published release"):
            await dsh.fetch_release(client, "shaoyuanyu/ysy-paperdb")
