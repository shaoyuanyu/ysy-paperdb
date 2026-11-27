from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    and_,
    exists,
    false,
    func,
    or_,
    select,
    true,
)
from sqlalchemy.sql.elements import ColumnElement

from paperdb.db.models import (
    Author,
    Paper,
    PaperAuthor,
    PaperIdentifier,
    PaperState,
    RankScheme,
    ReadingStatus,
    Tag,
    VenueRank,
)
from paperdb.errors import ApiError

SUPPORTED_FIELDS = {
    "q",
    "title",
    "abstract",
    "year",
    "kind",
    "venue.id",
    "venue.abbrev",
    "venue.ccf.level",
    "venue.ccf.field",
    "venue.cas.level",
    "venue.cas.top",
    "tag",
    "tags",
    "collection",
    "collections",
    "status",
    "priority",
    "rating",
    "starred",
    "archived",
    "hasPdf",
    "unfiled",
    "hasCode",
    "author.id",
    "author.name",
    "author.isMe",
    "addedAt",
    "updatedAt",
    "doi",
    "arxiv",
}
OPERATORS = {
    "eq",
    "ne",
    "in",
    "not_in",
    "between",
    "gte",
    "lte",
    "contains",
    "starts_with",
    "has_any",
    "has_all",
    "exists",
    "is_null",
}


def _fail(detail: str) -> ApiError:
    return ApiError(422, "invalid_filter", detail)


def validate_filter(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or payload.get("version") not in (None, 1):
        raise _fail("filter.version must be 1")
    rules = payload.get("rules") or []
    groups = payload.get("groups") or []
    if not isinstance(rules, list) or len(rules) > 50:
        raise _fail("filter.rules must be a list of <= 50 rules")
    if not isinstance(groups, list) or len(groups) > 10:
        raise _fail("filter.groups must be a list of <= 10 groups")
    if payload.get("match", "all") not in {"all", "any"}:
        raise _fail("filter.match must be all or any")
    for rule in [*rules, *(item for group in groups for item in (group.get("rules") or []))]:
        if not isinstance(rule, dict):
            raise _fail("filter rule must be an object")
        field = rule.get("field")
        op = rule.get("op")
        if field not in SUPPORTED_FIELDS:
            raise _fail(f"unsupported filter field: {field}")
        if op not in OPERATORS:
            raise _fail(f"unsupported filter operator: {op}")
        if op in {"in", "not_in", "has_any", "has_all", "between"} and not isinstance(
            rule.get("value"), list
        ):
            raise _fail(f"{op} needs an array value")
        if op == "between" and len(rule.get("value") or []) != 2:
            raise _fail("between needs exactly two values")


def _values(rule: dict[str, Any]) -> list[Any]:
    value = rule.get("value")
    return value if isinstance(value, list) else [value]


def _comparison(column, op: str, value: Any) -> ColumnElement:
    if op == "eq":
        return column == value
    if op == "ne":
        return column != value
    if op == "in":
        return column.in_(value)
    if op == "not_in":
        return column.not_in(value)
    if op == "between":
        return column.between(value[0], value[1])
    if op == "gte":
        return column >= value
    if op == "lte":
        return column <= value
    raise _fail(f"operator {op} not supported for this field")


def _text(column, op: str, value: Any) -> ColumnElement:
    if op == "contains":
        return func.lower(column).like(f"%{str(value).lower()}%")
    if op == "starts_with":
        return func.lower(column).like(f"{str(value).lower()}%")
    if op in {"eq", "ne"}:
        return (
            func.lower(column) == str(value).lower()
            if op == "eq"
            else func.lower(column) != str(value).lower()
        )
    if op == "in":
        return func.lower(column).in_([str(item).lower() for item in value])
    if op == "not_in":
        return func.lower(column).not_in([str(item).lower() for item in value])
    raise _fail(f"operator {op} not supported for text")


def _state_condition(op: str, value: Any):
    if op == "eq":
        return Paper.state.has(PaperState.status == ReadingStatus(str(value)))
    if op == "ne":
        return Paper.state.has(PaperState.status != ReadingStatus(str(value)))
    if op == "in":
        return Paper.state.has(PaperState.status.in_([ReadingStatus(str(item)) for item in value]))
    if op == "not_in":
        return Paper.state.has(
            PaperState.status.not_in([ReadingStatus(str(item)) for item in value])
        )
    raise _fail(f"operator {op} not supported for status")


def _parse_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise _fail(f"invalid date: {value}") from exc


def _compile_rule(rule: dict[str, Any]) -> ColumnElement:
    field = rule["field"]
    op = rule["op"]
    value = rule.get("value")

    if field == "q":
        pattern = f"%{str(value).lower()}%"
        return or_(
            func.lower(Paper.title).like(pattern),
            func.lower(Paper.paper_author_names).like(pattern),
            func.lower(func.coalesce(Paper.abstract, "")).like(pattern),
        )
    if field == "title":
        return _text(Paper.title, op, value)
    if field == "abstract":
        return _text(func.coalesce(Paper.abstract, ""), op, value)
    if field == "year":
        return _comparison(Paper.year, op, value)
    if field == "kind":
        return _comparison(Paper.kind, op, value)
    if field == "venue.id":
        return _comparison(Paper.venue_id, op, value)
    if field == "venue.abbrev":
        from paperdb.db.models import Venue

        return Paper.venue.has(_text(Venue.abbrev, op, value))
    if field in {"venue.ccf.level", "venue.ccf.field", "venue.cas.level", "venue.cas.top"}:
        scheme = RankScheme.ccf if "ccf" in field else RankScheme.cas
        conditions = [
            VenueRank.venue_id == Paper.venue_id,
            VenueRank.scheme == scheme,
            or_(VenueRank.valid_from.is_(None), VenueRank.valid_from <= Paper.year),
        ]
        if field == "venue.ccf.level":
            conditions.append(_comparison(VenueRank.level, op, value))
            conditions.append(VenueRank.field_kind == "ccf_field")
        elif field == "venue.ccf.field":
            conditions.append(or_(VenueRank.field_key == value, VenueRank.field_name == value))
            conditions.append(VenueRank.field_kind == "ccf_field")
        elif field == "venue.cas.level":
            conditions.append(_comparison(VenueRank.level, op, value))
            conditions.append(VenueRank.field_kind == "cas_major")
        elif field == "venue.cas.top":
            conditions.append(VenueRank.is_top == bool(value))
            conditions.append(VenueRank.field_kind == "cas_major")
        return exists(select(1).select_from(VenueRank).where(*conditions))
    if field in {"tag", "tags"}:
        paths = [str(item) for item in _values(rule)]
        if field == "tag":
            paths = paths[:1]
        if op in {"eq", "contains", "starts_with"}:
            paths = [str(value)]
        if op == "has_any":
            return Paper.tags.any(Tag.path.in_(paths))
        if op == "has_all":
            return and_(*[Paper.tags.any(Tag.path == path) for path in paths])
        if op == "not_in":
            return ~Paper.tags.any(Tag.path.in_(paths))
        return Paper.tags.any(Tag.path.in_(paths))
    if field in {"collection", "collections"}:
        from paperdb.db.models import Collection

        ids = _values(rule)
        if op == "not_in":
            return ~Paper.collections.any(Collection.id.in_(ids))
        return Paper.collections.any(Collection.id.in_(ids))
    if field == "status":
        return _state_condition(op, value)
    if field == "priority":
        return Paper.state.has(_comparison(PaperState.priority, op, value))
    if field == "rating":
        return Paper.state.has(_comparison(PaperState.rating, op, value))
    if field == "starred":
        if bool(value):
            return Paper.state.has(PaperState.starred_at.is_not(None))
        return ~Paper.state.has(PaperState.starred_at.is_not(None))
    if field == "archived":
        if bool(value):
            return Paper.state.has(PaperState.archived_at.is_not(None))
        return ~Paper.state.has(PaperState.archived_at.is_not(None))
    if field == "hasPdf":
        return false() if value else true()
    if field == "unfiled":
        return ~Paper.collections.any() if bool(value) else Paper.collections.any()
    if field == "hasCode":
        return false()
    if field in {"author.id", "author.name", "author.isMe"}:
        if field == "author.isMe":
            return Paper.authors.any(PaperAuthor.author.has(Author.is_me == bool(value)))
        if field == "author.id":
            return Paper.authors.any(PaperAuthor.author_id.in_(_values(rule)))
        return Paper.authors.any(PaperAuthor.author.has(_text(Author.display_name, op, value)))
    if field in {"addedAt", "updatedAt"}:
        column = Paper.created_at if field == "addedAt" else Paper.updated_at
        if op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise _fail("between needs exactly two dates")
            return column.between(_parse_date(value[0]), _parse_date(value[1]))
        return _comparison(column, op, _parse_date(value))
    if field in {"doi", "arxiv"}:
        return Paper.identifiers.any(
            and_(
                PaperIdentifier.scheme == field,
                PaperIdentifier.normalized.in_([str(item).lower() for item in _values(rule)]),
            )
        )
    raise _fail(f"unsupported filter field: {field}")


def compile_filter(payload: dict[str, Any] | None) -> ColumnElement:
    if not payload:
        return true()
    validate_filter(payload)
    rules = payload.get("rules") or []
    groups = payload.get("groups") or []
    match = payload.get("match", "all")
    rule_conditions = [_compile_rule(rule) for rule in rules]
    group_conditions = []
    for group in groups:
        conditions = [_compile_rule(rule) for rule in group.get("rules") or []]
        if conditions:
            group_conditions.append(
                and_(*conditions) if group.get("match", "all") == "all" else or_(*conditions)
            )
    combined = rule_conditions + group_conditions
    if not combined:
        return true()
    return and_(*combined) if match == "all" else or_(*combined)
