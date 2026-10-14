"""LCX API errors in the envelope the client actually reads.

The LCX SDK looks for ``code`` and ``message`` at the **top level** of an error body
and shows the code next to its own generic dialog number (the client displays
``30000-LCX_INVALID_TRANSFER_ID_OR_PASSWORD``, for example). FastAPI's ``HTTPException``
wraps whatever you give it in a ``detail`` object, so a 401 raised that way reached the
client with no readable code and the dialog showed a bare ``30000`` with nothing to act
on or report (2026-10-13).

Every handler in this router returns ``{"code": ..., "message": ...}`` for its own
error cases; this keeps the raised ones identical.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("dankagu.lcx.errors")


class LcxApiError(Exception):
    """An LCX error rendered as ``{"code": ..., "message": ...}``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


async def lcx_api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`LcxApiError` with the top-level envelope the SDK parses."""
    assert isinstance(exc, LcxApiError)
    logger.warning(
        "⚠️  [LCX] %s %s -> %d %s",
        request.method,
        request.url.path,
        exc.status_code,
        exc.code,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )
