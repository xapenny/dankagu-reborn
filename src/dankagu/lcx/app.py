"""FastAPI application for LCX authentication service with detailed request logging."""

import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from dankagu.core.database import init_db
from dankagu.lcx.router import router as lcx_router

logger = logging.getLogger("dankagu.http")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()
    yield


def create_lcx_app() -> FastAPI:
    app = FastAPI(
        title="DanKagu Reborn LCX Service",
        description="LCX Authentication Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable[[Request], Response]) -> Response:
        client_host = request.client.host if request.client else "unknown"
        start_time = time.perf_counter()

        # Fast path for asset requests: stream directly, do not log payload or buffer memory
        if request.url.path.startswith(("/assets", "/ver", "/static")):
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000
            if response.status_code >= 400:
                logger.warning(
                    "⚠️  [Asset %d] %s %s -> %d (%0.1fms)",
                    response.status_code,
                    request.method,
                    request.url.path,
                    response.status_code,
                    duration_ms,
                )
            return response

        req_body = await request.body()
        req_body_str = req_body.decode("utf-8", errors="replace").strip() if req_body else "(empty)"

        logger.info(
            "📥 [LCX HTTP REQ] %s %s from %s (Host: %s)\n   Payload: %s",
            request.method,
            request.url.path,
            client_host,
            request.headers.get("host", ""),
            req_body_str[:1000] if len(req_body_str) > 1000 else req_body_str,
        )

        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000

        resp_body = b""
        async for chunk in response.body_iterator:
            resp_body += chunk

        resp_body_str = (
            resp_body.decode("utf-8", errors="replace").strip() if resp_body else "(empty)"
        )
        new_response = Response(
            content=resp_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

        if response.status_code == 404:
            logger.warning(
                "⚠️  [LCX HTTP 404] %s %s -> %d (%0.1fms)\n   Response: %s",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                resp_body_str[:500],
            )
        elif response.status_code >= 500:
            logger.error(
                "💥 [LCX HTTP %d] %s %s -> %d (%0.1fms)\n   Response: %s",
                response.status_code,
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                resp_body_str[:500],
            )
        else:
            logger.info(
                "📤 [LCX HTTP RESP] %s %s -> %d (%0.1fms)\n   Response: %s",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                resp_body_str[:1000] if len(resp_body_str) > 1000 else resp_body_str,
            )
        return new_response

    app.include_router(lcx_router)

    return app
