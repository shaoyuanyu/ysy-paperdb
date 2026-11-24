from fastapi import APIRouter

from paperdb.api.v1 import (
    authors,
    collections,
    exports,
    imports,
    papers,
    projects,
    rules,
    system,
    tags,
    tokens,
    venues,
    views,
)

api_router = APIRouter(prefix="/v1")
api_router.include_router(system.router)
api_router.include_router(papers.router)
api_router.include_router(venues.router)
api_router.include_router(authors.router)
api_router.include_router(tokens.router)
api_router.include_router(exports.router)
api_router.include_router(imports.router)
api_router.include_router(collections.router)
api_router.include_router(tags.router)
api_router.include_router(views.router)
api_router.include_router(projects.router)
api_router.include_router(rules.router)

__all__ = ["api_router"]
