from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.auth.scopes import ALL_SCOPES
from paperdb.auth.tokens import TokenPrincipal, authenticate, extract_bearer, require_scope
from paperdb.auth.totp import verify_totp
from paperdb.config import Settings, get_settings
from paperdb.db.session import get_session
from paperdb.errors import ApiError


async def get_principal(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenPrincipal:
    token = extract_bearer(authorization)
    return await authenticate(session, token, settings)


def scoped(scope: str):
    # 未知 scope 会让该端点对所有令牌恒 403，而且很难从日志看出来；
    # 在路由注册时直接炸掉（进程起不来），比线上静默失效安全。
    if scope not in ALL_SCOPES:
        raise ValueError(f"unknown scope: {scope}")

    async def dependency(principal: TokenPrincipal = Depends(get_principal)) -> TokenPrincipal:
        require_scope(principal, scope)
        return principal

    return dependency


async def require_totp(
    principal: TokenPrincipal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
    x_totp_code: str | None = Header(default=None, alias="X-TOTP-Code"),
) -> None:
    """Gate destructive operations behind a user-approved TOTP code.

    Only personal tokens (agent-facing) are gated: the service token is the
    server-to-server web proxy whose secret never reaches a model. Fail-closed
    when PAPERDB_TOTP_SECRET is unset.
    """
    if principal.kind == "service":
        return
    if not settings.totp_secret:
        raise ApiError(
            503,
            "totp_not_configured",
            "Destructive operations require TOTP; PAPERDB_TOTP_SECRET is not set",
        )
    if not x_totp_code:
        raise ApiError(
            428,
            "totp_required",
            "A current 6-digit TOTP code is required for destructive operations "
            "(X-TOTP-Code header)",
        )
    if not verify_totp(settings.totp_secret, x_totp_code):
        raise ApiError(
            403,
            "totp_invalid",
            "Invalid or expired TOTP code",
        )
