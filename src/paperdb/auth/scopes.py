from __future__ import annotations

ALL_SCOPES: frozenset[str] = frozenset(
    {
        "papers:read",
        "papers:write",
        "organize:write",
        "pdf:read",
        "pdf:write",
        "annotations:write",
        "exports:read",
        "admin",
    }
)

DEFAULT_PERSONAL_SCOPES: tuple[str, ...] = (
    "papers:read",
    "papers:write",
    "organize:write",
    "pdf:read",
    "pdf:write",
    "annotations:write",
    "exports:read",
)


def validate_scopes(scopes: list[str]) -> list[str]:
    unknown = sorted(set(scopes) - ALL_SCOPES)
    if unknown:
        raise ValueError(f"unknown scopes: {', '.join(unknown)}")
    return list(dict.fromkeys(scopes))
