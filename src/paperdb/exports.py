from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.auth.tokens import _EXPORT_RE, TokenPrincipal, hash_token
from paperdb.config import Settings
from paperdb.db.models import ExportToken
from paperdb.errors import ApiError


async def authenticate_export_token(
    session: AsyncSession, raw_token: str, settings: Settings
) -> ExportToken:
    match = _EXPORT_RE.match(raw_token)
    if not match:
        raise ApiError(404, "not_found", "Export link not found")
    prefix = match.group(1)
    record = await session.scalar(select(ExportToken).where(ExportToken.prefix == prefix))
    if record is None or record.revoked_at is not None:
        raise ApiError(404, "not_found", "Export link not found")
    if record.token_hash != hash_token(raw_token, settings.token_pepper):
        raise ApiError(404, "not_found", "Export link not found")
    record.last_accessed_at = datetime.now(UTC)
    record.access_count += 1
    await session.commit()
    return record


def export_scopes(principal: TokenPrincipal) -> bool:
    return "*" in principal.scopes or "exports:read" in principal.scopes
