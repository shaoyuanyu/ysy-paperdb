"""Host adapter packaging: build, verify, install, roll back.

These tests exercise the real archive formats — the same bytes the release
workflow publishes and ``paperdb adapter update`` installs — so a manifest or
path-mapping change fails here before it reaches a user's host.
"""

from __future__ import annotations

import dataclasses
import gzip
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from paperdb import adapters
from paperdb.adapters import base as adapter_base

REPO_ROOT = Path(__file__).resolve().parents[1]
DSH = adapters.get_adapter("dsh")


def _tar_gz(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(body))
    return gzip.compress(buffer.getvalue(), mtime=0)


def _release(version: str, sha: str | None) -> adapters.Release:
    name = DSH.asset_name(version)
    return adapters.Release(
        version=version,
        tag=f"v{version}",
        published_at="2026-11-28T00:00:00Z",
        prerelease=False,
        notes=f"notes for {version}",
        page_url=f"https://github.com/shaoyuanyu/ysy-paperdb/releases/tag/v{version}",
        asset_name=name,
        asset_url=f"https://example.test/{name}",
        asset_sha256=sha,
        checksums_url="https://example.test/SHA256SUMS",
    )


def _built(tmp_path: Path, version: str) -> tuple[bytes, str]:
    paths = adapters.build_asset(DSH, REPO_ROOT, tmp_path / f"dist-{version}", version=version)
    data = paths["asset"].read_bytes()
    return data, adapters.sha256_hex(data)


def _context(target: Path) -> dict[str, str]:
    return adapters.build_context(
        target,
        mcp_command="/opt/venv/bin/paperdb-mcp",
        base_url="https://library.example.test",
        repo="shaoyuanyu/ysy-paperdb",
    )


def _verified(tmp_path: Path, version: str) -> adapters.VerifiedAsset:
    data, sha = _built(tmp_path, version)
    return adapters.verify_asset(DSH, data, expected_sha256=sha, expected_version=version)


def test_build_install_rollback_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "adapter"
    asset = _verified(tmp_path, "0.2.0")
    _, sha = _built(tmp_path, "0.2.0")

    report = adapters.install(DSH, target, asset, _release("0.2.0", sha), context=_context(target))
    assert report["ok"] and report["to"] == "0.2.0"
    assert report["host"] == "dsh"
    assert report["restartRequired"] is True
    assert (target / "mcp/dsh-paperdb-mcp/package.json").is_file()
    rendered = (target / "mcp/dsh-paperdb-mcp/cordis.patch.yml").read_text(encoding="utf-8")
    assert "{{" not in rendered
    assert f'command: "{_context(target)["mcp_command"]}"' in rendered
    # The bundle must stay secret-free and host-neutral in what it passes on:
    # the token is read from a file at runtime, and the server needs no
    # knowledge of where the adapter itself is installed.
    assert "PAPERDB_MCP_TOKEN_FILE" in rendered
    assert "PAPERDB_MCP_TOKEN:" not in rendered
    assert "PAPERDB_ADAPTER_DIR" not in rendered
    assert "PAPERDB_DSH_INTEGRATION_DIR" not in rendered
    assert (target / "skills/my-paperdb/SKILL.md").read_text(encoding="utf-8").startswith("---")

    status = adapters.installed_status(DSH, target)
    assert status["version"] == "0.2.0"
    assert status["packageVersion"] == "0.2.0"
    assert status["tracked"] is True
    assert status["drifted"] == []
    assert {item["path"] for item in status["files"]} == set(DSH.install_map.values())

    first_skill = (target / "skills/my-paperdb/SKILL.md").read_bytes()

    newer = _verified(tmp_path, "0.3.0")
    _, newer_sha = _built(tmp_path, "0.3.0")
    second = adapters.install(
        DSH, target, newer, _release("0.3.0", newer_sha), context=_context(target)
    )
    assert second["to"] == "0.3.0"
    assert second["backup"] and Path(second["backup"]).is_dir()
    assert (target / ".paperdb-adapter/backups").is_dir()

    restored = adapters.rollback(DSH, target)
    assert restored["ok"] and restored["version"] == "0.2.0"
    assert restored["restartRequired"] is True
    assert (target / "skills/my-paperdb/SKILL.md").read_bytes() == first_skill
    package = json.loads((target / "mcp/dsh-paperdb-mcp/package.json").read_text(encoding="utf-8"))
    assert package["version"] == "0.2.0"
    assert adapters.installed_status(DSH, target)["version"] == "0.2.0"


def test_manifest_is_host_specific(tmp_path: Path) -> None:
    data, _ = _built(tmp_path, "0.2.0")
    other = dataclasses.replace(DSH, host="other-host")
    with pytest.raises(adapters.AdapterError, match="is for host 'dsh'"):
        adapters.verify_asset(other, data)


def test_asset_name_is_host_scoped(tmp_path: Path) -> None:
    paths = adapters.build_asset(DSH, REPO_ROOT, tmp_path / "dist", version="0.2.0")
    assert paths["asset"].name == "paperdb-adapter-dsh-0.2.0.tar.gz"
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["kind"] == "paperdb-adapter"
    assert manifest["host"] == "dsh"
    assert manifest["skill"] == {"name": "my-paperdb", "version": "0.2.0"}


def test_legacy_state_is_still_read(tmp_path: Path) -> None:
    target = tmp_path / "legacy"
    legacy = target / ".paperdb-update"
    legacy.mkdir(parents=True)
    (legacy / "state.json").write_text(
        json.dumps({"version": "0.1.0", "files": {}, "source": {}}), encoding="utf-8"
    )
    status = adapters.installed_status(DSH, target)
    assert status["tracked"] is True
    assert status["version"] == "0.1.0"

    # A new state file wins over the legacy one.
    current = target / ".paperdb-adapter"
    current.mkdir()
    (current / "state.json").write_text(
        json.dumps({"host": "dsh", "version": "0.2.0", "files": {}}), encoding="utf-8"
    )
    assert adapters.installed_status(DSH, target)["version"] == "0.2.0"


def test_state_from_another_host_is_ignored(tmp_path: Path) -> None:
    target = tmp_path / "foreign"
    state_dir = target / ".paperdb-adapter"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"host": "somewhere-else", "version": "9.9.9"}), encoding="utf-8"
    )
    assert adapters.installed_status(DSH, target)["tracked"] is False


def test_rollback_without_backup_fails(tmp_path: Path) -> None:
    with pytest.raises(adapters.AdapterError, match="no previous"):
        adapters.rollback(DSH, tmp_path / "empty")


def test_install_detects_local_drift(tmp_path: Path) -> None:
    target = tmp_path / "drift"
    asset = _verified(tmp_path, "0.2.0")
    _, sha = _built(tmp_path, "0.2.0")
    adapters.install(DSH, target, asset, _release("0.2.0", sha), context=_context(target))
    skill = target / "skills/my-paperdb/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    assert adapters.installed_status(DSH, target)["drifted"] == ["skills/my-paperdb/SKILL.md"]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "dry"
    asset = _verified(tmp_path, "0.2.0")
    _, sha = _built(tmp_path, "0.2.0")
    report = adapters.install(
        DSH, target, asset, _release("0.2.0", sha), context=_context(target), dry_run=True
    )
    assert report["dryRun"] is True
    assert report["wouldChange"] == sorted(DSH.install_map.values())
    assert not target.exists()


def test_asset_rejects_path_traversal(tmp_path: Path) -> None:
    data = _tar_gz({"../escape.txt": b"owned"})
    with pytest.raises(adapters.AdapterError, match="unsafe path"):
        adapters.verify_asset(DSH, data)


def test_asset_rejects_unexpected_files() -> None:
    data = _tar_gz({"notes.txt": b"hello"})
    with pytest.raises(adapters.AdapterError, match="unexpected file"):
        adapters.verify_asset(DSH, data)


def test_asset_rejects_digest_mismatch(tmp_path: Path) -> None:
    data, _ = _built(tmp_path, "0.2.0")
    with pytest.raises(adapters.AdapterError, match="digest mismatch"):
        adapters.verify_asset(DSH, data, expected_sha256="0" * 64)


def test_asset_rejects_manifest_hash_mismatch() -> None:
    files = {
        "bundle/package.json": b"{}",
        "bundle/cordis.patch.yml": b"x",
        "skills/my-paperdb/SKILL.md": b"y",
    }
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": adapter_base.MANIFEST_KIND,
        "host": "dsh",
        "version": "0.2.0",
        "tag": "v0.2.0",
        "files": [
            {"path": name, "sha256": "0" * 64, "size": len(body)} for name, body in files.items()
        ],
    }
    data = _tar_gz({adapter_base.MANIFEST_NAME: json.dumps(manifest).encode(), **files})
    with pytest.raises(adapters.AdapterError, match="hash mismatch"):
        adapters.verify_asset(DSH, data)


def test_asset_requires_manifest_version_to_match(tmp_path: Path) -> None:
    data, sha = _built(tmp_path, "0.2.0")
    with pytest.raises(adapters.AdapterError, match="does not match the release"):
        adapters.verify_asset(DSH, data, expected_sha256=sha, expected_version="0.3.0")


def test_version_ordering() -> None:
    assert adapters.parse_version("0.10.0") > adapters.parse_version("0.9.9")
    assert adapters.parse_version("1.0.0") > adapters.parse_version("1.0.0-rc.1")
    assert adapters.parse_version("v1.2.3") == adapters.parse_version("1.2.3")
    with pytest.raises(adapters.AdapterError):
        adapters.parse_version("1.2")


def test_render_bundle_quotes_values_and_rejects_unknown() -> None:
    rendered = adapters.render_bundle("a: {{value}}", {"value": 'x"y\\z'})
    assert rendered == 'a: "x\\"y\\\\z"'
    with pytest.raises(adapters.AdapterError, match="unknown placeholder"):
        adapters.render_bundle("a: {{nope}}", {})


def test_resolve_target_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(adapters.ADAPTER_DIR_ENV, raising=False)
    monkeypatch.delenv("PAPERDB_DSH_INTEGRATION_DIR", raising=False)
    assert DSH.default_target is not None
    assert adapters.resolve_target(DSH) == DSH.default_target.expanduser().resolve()

    # The host's legacy variable still works when the generic one is unset.
    monkeypatch.setenv("PAPERDB_DSH_INTEGRATION_DIR", str(tmp_path / "legacy"))
    assert adapters.resolve_target(DSH) == (tmp_path / "legacy").resolve()

    # The generic variable outranks the host-specific legacy one.
    monkeypatch.setenv(adapters.ADAPTER_DIR_ENV, str(tmp_path / "generic"))
    assert adapters.resolve_target(DSH) == (tmp_path / "generic").resolve()

    # An explicit argument outranks every variable.
    assert (
        adapters.resolve_target(DSH, str(tmp_path / "explicit"))
        == (tmp_path / "explicit").resolve()
    )

    with pytest.raises(adapters.AdapterError, match="absolute"):
        adapters.resolve_target(DSH, "relative/dir")


def test_unknown_host_is_rejected() -> None:
    with pytest.raises(adapters.AdapterError, match="unknown host"):
        adapters.get_adapter("no-such-host")


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
                "name": "paperdb-adapter-dsh-0.2.0.tar.gz",
                "browser_download_url": "https://example.test/asset.tar.gz",
                "digest": f"sha256:{digest}",
            },
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://example.test/SHA256SUMS",
            },
        ],
    }
    release = adapter_base.release_from_payload(payload, DSH)
    assert release.version == "0.2.0"
    assert release.asset_sha256 == digest
    assert release.checksums_url == "https://example.test/SHA256SUMS"
    assert release.notes == "release notes"
    assert adapters.release_url_repo(release.page_url) == "shaoyuanyu/ysy-paperdb"


def test_release_payload_without_asset_fails() -> None:
    payload = {"tag_name": "v0.2.0", "assets": [{"name": "paperdb-0.2.0.whl"}]}
    with pytest.raises(adapters.AdapterError, match="no paperdb-adapter-dsh"):
        adapter_base.release_from_payload(payload, DSH)


def test_release_payload_for_another_host_is_not_used() -> None:
    payload = {
        "tag_name": "v0.2.0",
        "assets": [
            {
                "name": "paperdb-adapter-vim-0.2.0.tar.gz",
                "browser_download_url": "https://example.test/asset.tar.gz",
            }
        ],
    }
    with pytest.raises(adapters.AdapterError, match="no paperdb-adapter-dsh"):
        adapter_base.release_from_payload(payload, DSH)


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
                        "name": "paperdb-adapter-dsh-0.2.0.tar.gz",
                        "browser_download_url": "https://example.test/a.tar.gz",
                        "digest": f"sha256:{'ab' * 32}",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        release = await adapters.fetch_release(client, "shaoyuanyu/ysy-paperdb", DSH)
    assert seen == ["/repos/shaoyuanyu/ysy-paperdb/releases/latest"]
    assert release.version == "0.2.0"
    assert release.asset_sha256 == "ab" * 32


async def test_fetch_release_reports_missing_release() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(adapters.AdapterError, match="no published release"):
            await adapters.fetch_release(client, "shaoyuanyu/ysy-paperdb", DSH)
