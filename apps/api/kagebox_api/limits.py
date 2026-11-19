"""Request-body cap applied before routing, so nothing is parsed (or authenticated) first.

FastAPI reads and parses a declared body before it resolves dependencies such as the current
user, so without this an anonymous client could make the API buffer and json-parse a body of
hundreds of megabytes. Every request body is capped at ``MAX_BODY_BYTES`` while it is read,
counting the bytes actually received (a missing or false ``Content-Length`` does not help), and
handed to the app only once it is complete. The only exemption is a ``multipart/form-data`` POST
to an upload route (chapter source, rights evidence): those stream to disk and carry their own
caps (``max_upload_bytes``; Caddy allows 210 MB there and 1 MB everywhere else).
"""

from __future__ import annotations

import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 1_000_000  # 1 MB, the same limit Caddy applies outside the upload routes
UPLOAD_PATHS = re.compile(r"^/api/(chapters/[^/]+/upload|projects/[^/]+/rights)$")
TOO_LARGE = {"detail": "request too large"}


def _exempt(scope: Scope, headers: Headers) -> bool:
    ctype = headers.get("content-type", "").lower()
    return (
        scope["method"] == "POST"
        and ctype.startswith("multipart/form-data")
        and UPLOAD_PATHS.match(scope["path"]) is not None
    )


class BodyLimit:
    """Pure ASGI middleware: refuse (413) any non-exempt body over ``limit`` bytes."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if _exempt(scope, headers):
            await self.app(scope, receive, send)
            return
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > self.limit):
            await JSONResponse(TOO_LARGE, status_code=413)(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body = msg.get("body", b"")
            size += len(body)
            if size > self.limit:
                await JSONResponse(TOO_LARGE, status_code=413)(scope, receive, send)
                return
            chunks.append(body)
            if not msg.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
