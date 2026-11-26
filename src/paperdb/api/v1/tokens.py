from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import require_totp, scoped
from paperdb.api.schemas import TokenCreate, TokenCreated, TokenOut
from paperdb.auth.scopes import validate_scopes
from paperdb.auth.tokens import TokenPrincipal, create_personal_token
from paperdb.config import Settings, get_settings
from paperdb.db.models import ApiToken
from paperdb.db.session import get_session
from paperdb.errors import ApiError

router = APIRouter(tags=["tokens"])


@router.post("/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: TokenCreate,
    principal: TokenPrincipal = Depends(scoped("admin")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenCreated:
    try:
        scopes = validate_scopes(payload.scopes)
    except ValueError as exc:
        raise ApiError(422, "invalid_scope", str(exc)) from exc
    plaintext, prefix, token_hash = create_personal_token(
        payload.name, scopes, settings.token_pepper
    )
    record = ApiToken(
        name=payload.name,
        prefix=prefix,
        token_hash=token_hash,
        scopes=scopes,
        expires_at=payload.expires_at,
    )
    session.add(record)
    await session.commit()
    return TokenCreated(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        scopes=list(record.scopes),
        token=plaintext,
        expires_at=record.expires_at,
    )


@router.get("/tokens", response_model=list[TokenOut])
async def list_tokens(
    principal: TokenPrincipal = Depends(scoped("admin")),
    session: AsyncSession = Depends(get_session),
) -> list[TokenOut]:
    rows = (await session.scalars(select(ApiToken).order_by(ApiToken.created_at.desc()))).all()
    return [
        TokenOut(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            scopes=list(row.scopes),
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("admin")),
    totp: None = Depends(require_totp),
    session: AsyncSession = Depends(get_session),
) -> Response:
    row = await session.get(ApiToken, token_id)
    if row is None:
        raise ApiError(404, "not_found", "Token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
