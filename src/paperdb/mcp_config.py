"""How this machine reaches the paperdb API over MCP (host-neutral).

Only two things vary per machine: the base URL and the bearer token.  Both are
shared by ``paperdb-mcp`` (the stdio server) and by the ``paperdb adapter``
commands, which verify TOTP against the same API.  Keeping them here means the
server module itself has no host-specific knowledge.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_URL_ENV = "PAPERDB_MCP_BASE_URL"
TOKEN_ENV = "PAPERDB_MCP_TOKEN"
TOKEN_FILE_ENV = "PAPERDB_MCP_TOKEN_FILE"

# The API only serves this machine in development; releases point elsewhere.
DEFAULT_BASE_URL = "http://127.0.0.1:9720"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "paperdb" / "mcp-token"


def base_url() -> str:
    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")


def token_candidates() -> list[Path]:
    """Where a token may live, in priority order."""
    candidates: list[Path] = []
    env_path = os.environ.get(TOKEN_FILE_ENV)
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(DEFAULT_TOKEN_FILE)
    return candidates


def load_token() -> str:
    """The API token: the environment, else the first readable token file.

    Keeping the secret out of an adapter bundle lets that bundle live in a
    public repository and be published as a release asset; the token stays in
    one git-ignored file (``PAPERDB_MCP_TOKEN_FILE``, default
    ``~/.config/paperdb/mcp-token``).
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    for candidate in token_candidates():
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""
