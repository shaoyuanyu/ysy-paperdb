from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StoredObject:
    backend: str
    key: str
    sha256: str
    byte_size: int


class StorageBackend(ABC):
    name: str

    @abstractmethod
    async def put_bytes(self, data: bytes, *, extension: str = "pdf") -> StoredObject:
        """Store bytes under a content-addressed key and return the object."""

    @abstractmethod
    async def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def size(self, key: str) -> int: ...

    @abstractmethod
    async def read_range(self, key: str, start: int, end: int) -> bytes: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def healthcheck(self) -> None:
        """Raise if the backend is not writable/readable."""


def content_key(sha256_hex: str, extension: str = "pdf") -> str:
    ext = extension.lstrip(".")
    return f"{sha256_hex[:2]}/{sha256_hex[2:4]}/{sha256_hex}.{ext}"
