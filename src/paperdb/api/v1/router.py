from fastapi import APIRouter

from paperdb.api.v1 import authors, papers, system, tokens, venues

api_router = APIRouter(prefix="/v1")
api_router.include_router(system.router)
api_router.include_router(papers.router)
api_router.include_router(venues.router)
api_router.include_router(authors.router)
api_router.include_router(tokens.router)

__all__ = ["api_router"]
