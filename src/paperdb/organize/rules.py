from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import (
    AutoRule,
    Collection,
    CollectionPaper,
    Paper,
    PaperState,
    PaperTag,
    ReadingStatus,
)
from paperdb.errors import ApiError
from paperdb.organize.filters import compile_filter, validate_filter
from paperdb.organize.tags import ensure_tag_path


async def list_rules(session: AsyncSession) -> list[AutoRule]:
    return list((await session.scalars(select(AutoRule).order_by(AutoRule.created_at))).all())


async def get_rule(session: AsyncSession, rule_id: uuid.UUID) -> AutoRule:
    rule = await session.get(AutoRule, rule_id)
    if rule is None:
        raise ApiError(404, "not_found", "Auto rule not found")
    return rule


def _validate_actions(actions: list[dict[str, Any]]) -> None:
    for action in actions:
        kind = action.get("type")
        if kind not in {"add_collection", "add_tag", "set_status"}:
            raise ApiError(422, "invalid_rule", f"unsupported action: {kind}")
        if kind == "add_collection" and not action.get("id"):
            raise ApiError(422, "invalid_rule", "add_collection needs id")
        if kind == "add_tag" and not action.get("path"):
            raise ApiError(422, "invalid_rule", "add_tag needs path")
        if kind == "set_status" and action.get("value") not in {
            item.value for item in ReadingStatus
        }:
            raise ApiError(422, "invalid_rule", "invalid set_status value")


async def create_rule(
    session: AsyncSession,
    *,
    name: str,
    filter_payload: dict[str, Any],
    actions: list[dict[str, Any]],
    run_on: list[str],
    enabled: bool,
) -> AutoRule:
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_rule", "name is required")
    validate_filter(filter_payload)
    _validate_actions(actions)
    rule = AutoRule(
        id=uuid.uuid4(),
        name=name,
        filter=filter_payload,
        actions=actions,
        run_on=run_on or ["manual"],
        enabled=enabled,
    )
    session.add(rule)
    await session.flush()
    return rule


async def update_rule(session: AsyncSession, rule: AutoRule, payload: dict[str, Any]) -> AutoRule:
    if payload.get("filter") is not None:
        validate_filter(payload["filter"])
        rule.filter = payload["filter"]
    if payload.get("actions") is not None:
        _validate_actions(payload["actions"])
        rule.actions = payload["actions"]
    for field in ("name", "run_on", "enabled"):
        if field in payload and payload[field] is not None:
            setattr(rule, field, payload[field])
    await session.flush()
    return rule


async def delete_rule(session: AsyncSession, rule: AutoRule) -> None:
    await session.delete(rule)


async def _apply_actions(session: AsyncSession, rule: AutoRule, paper_ids: list[uuid.UUID]) -> int:
    applied = 0
    for paper_id in paper_ids:
        for action in rule.actions:
            kind = action.get("type")
            if kind == "add_collection":
                collection = await session.get(Collection, uuid.UUID(str(action["id"])))
                if collection is None:
                    continue
                exists = await session.get(CollectionPaper, (collection.id, paper_id))
                if exists is None:
                    session.add(CollectionPaper(collection_id=collection.id, paper_id=paper_id))
            elif kind == "add_tag":
                tag = await ensure_tag_path(session, str(action["path"]))
                exists = await session.get(PaperTag, (paper_id, tag.id))
                if exists is None:
                    session.add(PaperTag(paper_id=paper_id, tag_id=tag.id))
            elif kind == "set_status":
                state = await session.get(PaperState, paper_id)
                if state is None:
                    session.add(
                        PaperState(
                            paper_id=paper_id,
                            status=ReadingStatus(str(action["value"])),
                        )
                    )
                else:
                    state.status = ReadingStatus(str(action["value"]))
        applied += 1
    return applied


async def apply_rule(session: AsyncSession, rule: AutoRule) -> int:
    condition = compile_filter(rule.filter)
    paper_ids = list(
        (await session.scalars(select(Paper.id).where(Paper.deleted_at.is_(None), condition))).all()
    )
    applied = await _apply_actions(session, rule, paper_ids)
    rule.last_run_at = datetime.now(UTC)
    await session.flush()
    return applied


async def apply_rules_on(session: AsyncSession, paper: Paper, trigger: str) -> int:
    rules = [
        rule
        for rule in (
            await session.scalars(select(AutoRule).where(AutoRule.enabled.is_(True)))
        ).all()
        if trigger in (rule.run_on or [])
    ]
    applied = 0
    for rule in rules:
        matches = await session.scalar(
            select(Paper.id).where(
                Paper.id == paper.id,
                Paper.deleted_at.is_(None),
                compile_filter(rule.filter),
            )
        )
        if matches is None:
            continue
        applied += await _apply_actions(session, rule, [paper.id])
        rule.last_run_at = datetime.now(UTC)
    await session.flush()
    return applied
