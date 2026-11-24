from __future__ import annotations

import asyncio
from typing import Any

import httpx

from paperdb.config import Settings


def build_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=8.0),
        follow_redirects=True,
        headers={
            "User-Agent": settings.arxiv_user_agent,
            "Accept": "application/json",
        },
    )


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            if attempt + 1 == attempts:
                return None
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code == 404:
            return None
        if response.status_code in {429, 500, 502, 503, 504}:
            if attempt + 1 == attempts:
                return None
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        if response.status_code >= 400:
            return None
        try:
            return response.json()
        except ValueError:
            return None
    return None


async def get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
) -> str | None:
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            if attempt + 1 == attempts:
                return None
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code == 404:
            return None
        if response.status_code in {429, 500, 502, 503, 504}:
            if attempt + 1 == attempts:
                return None
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        if response.status_code >= 400:
            return None
        return response.text
    return None
