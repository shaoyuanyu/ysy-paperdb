from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ApiError(Exception):
    status: int
    code: str
    title: str
    detail: str | None = None
    headers: dict[str, str] | None = None

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.detail or self.title}"
