from __future__ import annotations

from functools import lru_cache

from paperdb.config import get_settings
from paperdb.storage.base import StorageBackend, StoredObject, content_key
from paperdb.storage.local import LocalStorage

__all__ = ["LocalStorage", "StorageBackend", "StoredObject", "content_key", "get_storage"]


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_root)
    raise RuntimeError(f"storage backend not implemented: {settings.storage_backend}")
