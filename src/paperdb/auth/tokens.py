from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.config import Settings
from paperdb.db.models import ApiToken
from paperdb.errors import ApiError

PERSONAL_PREFIX = "pdb_pat_"
EXPORT_PREFIX = "pdb_ext_"
SERVICE_PREFIX = "pdb_svc_"
_PERSONAL_RE = re.compile(r"^pdb_pat_([0-9a-f]{8})_(.+)$")
_EXPORT_RE = re.compile(r"^pdb_ext_([0-9a-f]{8})_(.+)$")


@dataclass(slots=True)
class TokenPrincipal:
    kind: str  # service | personal
    name: str
    scopes: frozenset[str]
    token_id: str | None = None


def hash_token(token: str, pepper: str) -> str:
    return hashlib.sha256(f"{token}{pepper}".encode()).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def create_personal_token(name: str, scopes: list[str], pepper: str) -> tuple[str, str, str]:
    """Return (plaintext, prefix, hash). The plaintext is shown exactly once."""
    prefix = secrets.token_hex(4)
    secret = _new_secret()
    token = f"{PERSONAL_PREFIX}{prefix}_{secret}"
    return token, prefix, hash_token(token, pepper)


def create_export_token(name: str, pepper: str) -> tuple[str, str, str]:
    prefix = secrets.token_hex(4)
    secret = _new_secret()
    token = f"{EXPORT_PREFIX}{prefix}_{secret}"
    return token, prefix, hash_token(token, pepper)


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise ApiError(401, "unauthorized", "Missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "unauthorized", "Invalid Authorization header")
    return token.strip()


async def authenticate(session: AsyncSession, token: str, settings: Settings) -> TokenPrincipal:
    if settings.service_token and hmac.compare_digest(token, settings.service_token):
        return TokenPrincipal(
            kind="service",
            name="service",
            scopes=frozenset({"*"}),
        )

    match = _PERSONAL_RE.match(token)
    if not match:
        raise ApiError(401, "unauthorized", "Invalid token")
    prefix = match.group(1)
    record = await session.scalar(select(ApiToken).where(ApiToken.prefix == prefix))
    if record is None:
        raise ApiError(401, "unauthorized", "Invalid token")
    if record.revoked_at is not None:
        raise ApiError(401, "unauthorized", "Token revoked")
    now = datetime.now(UTC)
    if record.expires_at is not None and record.expires_at <= now:
        raise ApiError(401, "unauthorized", "Token expired")
    if not hmac.compare_digest(record.token_hash, hash_token(token, settings.token_pepper)):
        raise ApiError(401, "unauthorized", "Invalid token")

    await session.execute(update(ApiToken).where(ApiToken.id == record.id).values(last_used_at=now))
    await session.commit()
    return TokenPrincipal(
        kind="personal",
        name=record.name,
        scopes=frozenset(record.scopes),
        token_id=str(record.id),
    )


def require_scope(principal: TokenPrincipal, scope: str) -> None:
    if "*" in principal.scopes or scope in principal.scopes:
        return
    raise ApiError(403, "insufficient_scope", "Insufficient scope", f"requires {scope}")
