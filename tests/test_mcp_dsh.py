"""Updater tools through the MCP surface.

The GitHub side is faked (no network in tests), but everything after it is
real: the release asset is built from this repo, verified, rendered, installed
into a temp directory, and the TOTP gate goes through the in-process API.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

from paperdb import dsh_integration, mcp_server
from paperdb.auth.totp import current_totp, reset_failures
from paperdb.main import app

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}
SECRET = os.environ["PAPERDB_TOTP_SECRET"]
REPO_ROOT = Path(__file__).resolve().parents[1]


def _fake_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
) -> dsh_integration.Release:
    """Serve ``version`` as the latest release from a real built asset."""
    data = dsh_integration.build_release(REPO_ROOT, tmp_path / f"dist-{version}", version=version)[
        "asset"
    ].read_bytes()
    sha = dsh_integration.sha256_hex(data)
    release = dsh_integration.Release(
        version=version,
        tag=f"v{version}",
        published_at="2026-11-28T00:00:00Z",
        prerelease=False,
        notes=f"notes for {version}",
        page_url=f"https://github.com/shaoyuanyu/ysy-paperdb/releases/tag/v{version}",
        asset_name=f"paperdb-dsh-{version}.tar.gz",
        asset_url=f"https://example.test/paperdb-dsh-{version}.tar.gz",
        asset_sha256=sha,
        checksums_url=None,
    )

    async def fetch_release(
        client: httpx.AsyncClient,
        repo: str = dsh_integration.DEFAULT_REPO,
        *,
        version: str | None = None,
        token: str | None = None,
    ) -> dsh_integration.Release:
        return release

    async def download_bytes(
        client: httpx.AsyncClient,
        url: str,
        *,
        token: str | None = None,
        max_bytes: int = dsh_integration.MAX_TARBALL_BYTES,
    ) -> bytes:
        return data

    monkeypatch.setattr(dsh_integration, "fetch_release", fetch_release)
    monkeypatch.setattr(dsh_integration, "download_bytes", download_bytes)
    return release


def _personal_client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


async def test_status_reports_an_available_update(
    tmp_path: Path, mcp_api: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    monkeypatch.setenv(dsh_integration.INTEGRATION_DIR_ENV, str(target))
    _fake_github(monkeypatch, tmp_path, "0.2.0")

    status = json.loads(await mcp_server.dsh_integration_status())
    assert status["ok"] is True
    assert status["version"] is None
    assert status["updateAvailable"] is True
    assert status["latest"]["version"] == "0.2.0"
    assert status["latest"]["sha256"] == dsh_integration.sha256_hex(
        (tmp_path / "dist-0.2.0/paperdb-dsh-0.2.0.tar.gz").read_bytes()
    )


async def test_status_without_target_explains_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(dsh_integration.INTEGRATION_DIR_ENV, raising=False)
    status = json.loads(await mcp_server.dsh_integration_status())
    assert status["ok"] is False
    assert "PAPERDB_DSH_INTEGRATION_DIR" in status["error"]


async def test_update_installs_skill_and_bundle(
    tmp_path: Path, mcp_api: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    monkeypatch.setenv(dsh_integration.INTEGRATION_DIR_ENV, str(target))
    _fake_github(monkeypatch, tmp_path, "0.2.0")

    report = json.loads(await mcp_server.update_dsh_integration())
    assert report["ok"] is True
    assert report["to"] == "0.2.0"
    assert report["restartRequired"] is True
    assert (target / "skills/my-paperdb/SKILL.md").is_file()
    rendered = (target / "mcp/dsh-paperdb-mcp/cordis.patch.yml").read_text(encoding="utf-8")
    assert "{{" not in rendered
    assert str(target) in rendered

    after = json.loads(await mcp_server.dsh_integration_status())
    assert after["version"] == "0.2.0"
    assert after["updateAvailable"] is False
    assert after["drifted"] == []


async def test_personal_token_update_needs_totp(
    tmp_path: Path,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await client.post(
        "/v1/tokens",
        headers=SERVICE,
        json={"name": f"dsh-{uuid.uuid4().hex[:8]}", "scopes": ["papers:read"]},
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    monkeypatch.setattr(mcp_server, "_client", lambda: _personal_client(token))
    target = tmp_path / "target"
    monkeypatch.setenv(dsh_integration.INTEGRATION_DIR_ENV, str(target))
    _fake_github(monkeypatch, tmp_path, "0.2.0")
    reset_failures()

    with pytest.raises(RuntimeError, match="totp_required"):
        await mcp_server.update_dsh_integration()
    assert not target.exists()

    # A dry run previews without a code and still writes nothing.
    preview = json.loads(await mcp_server.update_dsh_integration(dry_run=True))
    assert preview["dryRun"] is True and preview["wouldChange"]
    assert not target.exists()

    report = json.loads(await mcp_server.update_dsh_integration(totp_code=current_totp(SECRET)))
    assert report["ok"] is True and report["to"] == "0.2.0"
    reset_failures()


async def test_downgrade_is_refused(
    tmp_path: Path, mcp_api: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    monkeypatch.setenv(dsh_integration.INTEGRATION_DIR_ENV, str(target))
    _fake_github(monkeypatch, tmp_path, "0.2.0")
    assert json.loads(await mcp_server.update_dsh_integration())["ok"] is True

    _fake_github(monkeypatch, tmp_path, "0.1.0")
    report = json.loads(await mcp_server.update_dsh_integration(version="0.1.0"))
    assert report["ok"] is False
    assert "older than the installed" in report["error"]


async def test_rollback_restores_the_previous_install(
    tmp_path: Path, mcp_api: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    monkeypatch.setenv(dsh_integration.INTEGRATION_DIR_ENV, str(target))
    _fake_github(monkeypatch, tmp_path, "0.2.0")
    assert json.loads(await mcp_server.update_dsh_integration())["ok"] is True
    _fake_github(monkeypatch, tmp_path, "0.3.0")
    assert json.loads(await mcp_server.update_dsh_integration())["ok"] is True
    assert json.loads(await mcp_server.dsh_integration_status())["version"] == "0.3.0"

    report = json.loads(await mcp_server.rollback_dsh_integration())
    assert report["ok"] is True
    assert report["version"] == "0.2.0"
    package = json.loads((target / "mcp/dsh-paperdb-mcp/package.json").read_text(encoding="utf-8"))
    assert package["version"] == "0.2.0"


def test_token_resolves_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bundle carries no secret; the token comes from a 0600 file."""
    token_file = tmp_path / "mcp-token"
    token_file.write_text("pdb_pat_from_file\n", encoding="utf-8")
    monkeypatch.delenv("PAPERDB_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PAPERDB_MCP_TOKEN_FILE", str(token_file))
    assert mcp_server._load_token() == "pdb_pat_from_file"

    monkeypatch.setenv("PAPERDB_MCP_TOKEN", "pdb_pat_from_env")
    assert mcp_server._load_token() == "pdb_pat_from_env"
