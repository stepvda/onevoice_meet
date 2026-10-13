"""Embedding the SPA in another app (One Voice Mail's Meet page).

The SPA turns on its embed mode only when it is framed by a parent whose
origin is listed here (and passed as `?parent=`), and then talks to that
parent with postMessage pinned to the origin. Caddy's `frame-ancestors`
(MEET_FRAME_ANCESTORS) is what actually lets the browser frame the page;
keep both lists in step. See docs/MAIL_INTEGRATION.md.
"""
from fastapi import APIRouter

from app.config import settings

router = APIRouter(prefix="/v1")


@router.get("/embed/config")
def embed_config() -> dict:
    """Public (read before sign-in): the origins allowed to embed the SPA."""
    return {"parent_origins": settings.embed_parent_origin_list}
