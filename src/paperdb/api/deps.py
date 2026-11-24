from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.auth.tokens import TokenPrincipal, authenticate, extract_bearer, require_scope
from paperdb.config import Settings, get_settings
from paperdb.db.session import get_session


async def get_principal(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenPrincipal:
    token = extract_bearer(authorization)
    return await authenticate(session, token, settings)


def scoped(scope: str):
    async def dependency(principal: TokenPrincipal = Depends(get_principal)) -> TokenPrincipal:
        require_scope(principal, scope)
        return principal

    return dependency
