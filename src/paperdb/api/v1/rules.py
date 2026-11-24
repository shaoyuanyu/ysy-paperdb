from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import AutoRuleCreate, AutoRuleOut, AutoRuleUpdate
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.organize.rules import (
    apply_rule,
    create_rule,
    delete_rule,
    get_rule,
    list_rules,
    update_rule,
)

router = APIRouter(prefix="/auto-rules", tags=["auto-rules"])


def _out(rule) -> AutoRuleOut:
    return AutoRuleOut(
        id=rule.id,
        name=rule.name,
        filter=rule.filter,
        actions=rule.actions,
        run_on=rule.run_on,
        enabled=rule.enabled,
        last_run_at=rule.last_run_at,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.get("", response_model=list[AutoRuleOut])
async def list_endpoint(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[AutoRuleOut]:
    return [_out(rule) for rule in await list_rules(session)]


@router.post("", response_model=AutoRuleOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: AutoRuleCreate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> AutoRuleOut:
    rule = await create_rule(
        session,
        name=payload.name,
        filter_payload=payload.filter,
        actions=payload.actions,
        run_on=payload.run_on,
        enabled=payload.enabled,
    )
    await session.commit()
    return _out(rule)


@router.patch("/{rule_id}", response_model=AutoRuleOut)
async def update_endpoint(
    rule_id: uuid.UUID,
    payload: AutoRuleUpdate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> AutoRuleOut:
    rule = await get_rule(session, rule_id)
    await update_rule(session, rule, payload.model_dump(exclude_unset=True))
    await session.commit()
    return _out(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    rule_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await delete_rule(session, await get_rule(session, rule_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{rule_id}/apply")
async def apply_endpoint(
    rule_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    rule = await get_rule(session, rule_id)
    applied = await apply_rule(session, rule)
    await session.commit()
    return {"applied": applied}
