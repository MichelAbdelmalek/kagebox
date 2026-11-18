from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException, Request, Response, status

from kagebox_api.db import audit
from kagebox_api.deps import DB, CurrentUser, state
from kagebox_api.security import BadMediaToken, verify_media
from pipeline.common.storage import parse_ref

router = APIRouter(prefix="/media", tags=["media"])

# Buckets whose every read is written to the audit log.
AUDITED_BUCKETS = frozenset({"consent", "evidence"})


@router.get("/{token}")
def get_media(token: str, request: Request, user: CurrentUser, db: DB) -> Response:
    """Serve one object for a signed, user-bound token that expires within 5 minutes."""
    st = state(request)
    try:
        ref = verify_media(st.media_key, token, user.id)
        bucket, _ = parse_ref(ref)
    except (BadMediaToken, ValueError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "link is invalid or has expired") from exc
    try:
        data = st.store.get(ref)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from exc
    if bucket in AUDITED_BUCKETS:
        audit(db, user.id, "media.read", ref)
        db.commit()
    mime = mimetypes.guess_type(ref)[0] or "application/octet-stream"
    return Response(
        data,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
