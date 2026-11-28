"""``paperdb adapter`` command tests.

The adapter writes host configuration files on the owner's machine, so these
tests pin the two properties that matter: nothing is written without a verified
TOTP code, and an update is always digest-verified, backed up, and reversible.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from paperdb import adapters, mcp_config
from paperdb.adapters import cli as adapter_cli
from paperdb.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
DSH = adapters.get_adapter("dsh")
TOTP = "123456"


def _built(tmp_path: Path, version: str) -> tuple[bytes, str]:
    paths = adapters.build_asset(DSH, REPO_ROOT, tmp_path / f"dist-{version}", version=version)
    data = paths["asset"].read_bytes()
    return data, adapters.sha256_hex(data)


def _release(version: str, sha: str) -> adapters.Release:
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


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], adapters.Release]:
    """Serve releases from assets built out of this checkout."""
    store: dict[str, tuple[bytes, adapters.Release]] = {}

    def add(version: str) -> adapters.Release:
        data, sha = _built(tmp_path, version)
        release = _release(version, sha)
        store[version] = (data, release)
        return release

    async def fetch_release(
        client: httpx.AsyncClient,
        repo: str,
        adapter: adapters.Adapter,
        *,
        version: str | None = None,
        token: str | None = None,
    ) -> adapters.Release:
        if version:
            return store[version][1]
        return store[max(store, key=adapters.parse_version)][1]

    async def download_bytes(
        client: httpx.AsyncClient,
        url: str,
        *,
        token: str | None = None,
        max_bytes: int = 0,
    ) -> bytes:
        for data, release in store.values():
            if release.asset_url == url:
                return data
        raise AssertionError(f"unexpected download: {url}")

    monkeypatch.setattr(adapters, "fetch_release", fetch_release)
    monkeypatch.setattr(adapters, "download_bytes", download_bytes)
    return add


@pytest.fixture
def totp(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Verify codes against a fake API that only accepts ``TOTP``."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/totp/verify":
            raise AssertionError(f"unexpected request: {request.url}")
        code = request.headers.get("X-TOTP-Code", "")
        seen.append(code)
        if code == TOTP:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(403, json={"code": "totp_invalid", "detail": "bad code"})

    monkeypatch.setattr(
        adapter_cli,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(mcp_config, "load_token", lambda: "pdb_pat_test")
    return seen


def _json(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


def test_update_without_totp_writes_nothing(
    tmp_path: Path, github: Callable[[str], adapters.Release], capsys: pytest.CaptureFixture[str]
) -> None:
    github("0.2.0")
    target = tmp_path / "adapter"
    code = main(["adapter", "update", "--host", "dsh", "--dir", str(target), "--json"])
    payload = _json(capsys)
    assert code == 1
    assert payload["ok"] is False
    assert "TOTP" in payload["error"]
    assert not target.exists()


def test_update_with_totp_installs_skill_and_bundle(
    tmp_path: Path,
    github: Callable[[str], adapters.Release],
    totp: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    github("0.2.0")
    target = tmp_path / "adapter"
    code = main(
        [
            "adapter",
            "update",
            "--host",
            "dsh",
            "--dir",
            str(target),
            "--totp",
            TOTP,
            "--json",
        ]
    )
    report = _json(capsys)
    assert code == 0 and report["ok"] is True
    assert report["to"] == "0.2.0" and report["restartRequired"] is True
    assert totp == [TOTP]
    assert (target / "mcp/dsh-paperdb-mcp/cordis.patch.yml").is_file()
    assert (target / "skills/my-paperdb/SKILL.md").is_file()

    status = adapters.installed_status(DSH, target)
    assert status["version"] == "0.2.0"
    assert status["tracked"] is True


def test_rejected_totp_writes_nothing(
    tmp_path: Path,
    github: Callable[[str], adapters.Release],
    totp: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    github("0.2.0")
    target = tmp_path / "adapter"
    code = main(
        [
            "adapter",
            "update",
            "--host",
            "dsh",
            "--dir",
            str(target),
            "--totp",
            "000000",
            "--json",
        ]
    )
    payload = _json(capsys)
    assert code == 1 and payload["ok"] is False
    assert "totp_invalid" in payload["error"]
    assert not target.exists()


def test_dry_run_needs_no_totp_and_writes_nothing(
    tmp_path: Path, github: Callable[[str], adapters.Release], capsys: pytest.CaptureFixture[str]
) -> None:
    github("0.2.0")
    target = tmp_path / "adapter"
    code = main(["adapter", "update", "--host", "dsh", "--dir", str(target), "--dry-run", "--json"])
    report = _json(capsys)
    assert code == 0
    assert report["wouldChange"] == sorted(DSH.install_map.values())
    assert not target.exists()


def test_dry_run_reports_the_same_files_as_a_real_update(
    tmp_path: Path,
    github: Callable[[str], adapters.Release],
    totp: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    github("0.2.0")
    target = tmp_path / "adapter"
    code = main(["adapter", "update", "--host", "dsh", "--dir", str(target), "--dry-run", "--json"])
    preview = _json(capsys)
    assert code == 0
    code = main(
        ["adapter", "update", "--host", "dsh", "--dir", str(target), "--totp", TOTP, "--json"]
    )
    assert code == 0
    _json(capsys)
    assert sorted(preview["wouldChange"]) == sorted(
        item["path"] for item in adapters.installed_status(DSH, target)["files"]
    )


def test_up_to_date_short_circuit(
    tmp_path: Path,
    github: Callable[[str], adapters.Release],
    totp: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    github("0.2.0")
    target = tmp_path / "adapter"
    args = ["adapter", "update", "--host", "dsh", "--dir", str(target), "--totp", TOTP, "--json"]
    assert main(args) == 0
    _json(capsys)
    assert main(args) == 0
    assert _json(capsys)["upToDate"] is True


def test_downgrade_is_refused(
    tmp_path: Path,
    github: Callable[[str], adapters.Release],
    totp: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    github("0.1.0")
    github("0.2.0")
    target = tmp_path / "adapter"
    assert (
        main(["adapter", "update", "--host", "dsh", "--dir", str(target), "--totp", TOTP, "--json"])
        == 0
    )
    _json(capsys)
    code = main(
        [
            "adapter",
            "update",
            "--host",
            "dsh",
            "--dir",
            str(target),
            "--version",
            "0.1.0",
            "--totp",
            TOTP,
            "--json",
        ]
    )
    payload = _json(capsys)
    assert code == 1
    assert "older than the installed" in payload["error"]
    assert adapters.installed_status(DSH, target)["version"] == "0.2.0"


def test_rollback_restores_the_previous_install(
    tmp_path: Path,
    github: Callable[[str], adapters.Release],
    totp: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    github("0.2.0")
    target = tmp_path / "adapter"
    assert (
        main(["adapter", "update", "--host", "dsh", "--dir", str(target), "--totp", TOTP, "--json"])
        == 0
    )
    _json(capsys)
    skill = (target / "skills/my-paperdb/SKILL.md").read_bytes()

    github("0.3.0")
    assert (
        main(["adapter", "update", "--host", "dsh", "--dir", str(target), "--totp", TOTP, "--json"])
        == 0
    )
    _json(capsys)
    assert adapters.installed_status(DSH, target)["version"] == "0.3.0"

    code = main(
        ["adapter", "rollback", "--host", "dsh", "--dir", str(target), "--totp", TOTP, "--json"]
    )
    report = _json(capsys)
    assert code == 0 and report["version"] == "0.2.0"
    assert (target / "skills/my-paperdb/SKILL.md").read_bytes() == skill
    assert adapters.installed_status(DSH, target)["version"] == "0.2.0"


def test_check_reports_an_available_update(
    tmp_path: Path, github: Callable[[str], adapters.Release], capsys: pytest.CaptureFixture[str]
) -> None:
    release = github("0.2.0")
    target = tmp_path / "adapter"
    code = main(["adapter", "check", "--host", "dsh", "--dir", str(target), "--json"])
    payload = _json(capsys)
    assert code == 0 and payload["ok"] is True
    assert payload["updateAvailable"] is True
    assert payload["latest"]["version"] == "0.2.0"
    assert payload["latest"]["sha256"] == release.asset_sha256


def test_check_ignores_the_host_env_of_another_tool(
    tmp_path: Path,
    github: Callable[[str], adapters.Release],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale DSH variable must not leak into a different host's command."""
    github("0.2.0")
    monkeypatch.setenv("PAPERDB_DSH_INTEGRATION_DIR", str(tmp_path / "stale"))
    target = tmp_path / "adapter"
    assert main(["adapter", "status", "--host", "dsh", "--dir", str(target), "--json"]) == 0
    assert _json(capsys)["target"] == str(target.resolve())


def test_status_is_offline_and_reports_missing_install(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["adapter", "status", "--host", "dsh", "--dir", str(tmp_path / "empty"), "--json"])
    payload = _json(capsys)
    assert code == 0
    assert payload["tracked"] is False
    assert payload["version"] is None
    assert all(not item["installed"] for item in payload["files"])


def test_unknown_host_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["adapter", "status", "--host", "no-such-host", "--json"])
    assert code == 2
    assert "no-such-host" in _json(capsys)["error"]

    code = main(["adapter", "status", "--host", "no-such-host"])
    assert code == 2
    assert "no-such-host" in capsys.readouterr().err


def test_list_shows_the_known_hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(adapters.ADAPTER_DIR_ENV, str(tmp_path))
    assert adapters.list_hosts() == ["dsh"]
    assert adapters.DEFAULT_HOST == "dsh"


def test_token_resolves_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "mcp-token"
    token_file.write_text("pdb_pat_from_file\n", encoding="utf-8")
    monkeypatch.delenv(mcp_config.TOKEN_ENV, raising=False)
    monkeypatch.setenv(mcp_config.TOKEN_FILE_ENV, str(token_file))
    assert mcp_config.load_token() == "pdb_pat_from_file"

    monkeypatch.setenv(mcp_config.TOKEN_ENV, "pdb_pat_from_env")
    assert mcp_config.load_token() == "pdb_pat_from_env"
