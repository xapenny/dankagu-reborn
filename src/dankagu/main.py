"""Danmaku Kagura backend preservation unified orchestrator entrypoint."""

import asyncio
import io
import logging
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from dankagu.assets.generator import generate_all_manifest_assets
from dankagu.assets.server import router as asset_router
from dankagu.config import settings
from dankagu.core.certs import generate_certificates
from dankagu.core.database import init_db
from dankagu.core.net import detect_local_ip
from dankagu.grpc.server import create_grpc_server
from dankagu.lcx.app import create_lcx_app

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
out_stream = (
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer")
    else sys.stdout
)
console_handler = logging.StreamHandler(out_stream)
console_handler.setFormatter(log_formatter)
file_handler = logging.FileHandler("server.log", encoding="utf-8", mode="a")
file_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[console_handler, file_handler],
)
logger = logging.getLogger("dankagu")


def ensure_certificates() -> None:
    cert_path = settings.certs_dir / "server.crt"
    key_path = settings.certs_dir / "server.key"
    if not cert_path.exists() or not key_path.exists():
        logger.info("TLS certificates not found. Generating fresh certificates...")
        generate_certificates(settings.certs_dir)


async def run_servers() -> None:
    """Launch the LCX (443 & 9443), gRPC, and asset/portal servers concurrently."""
    # 0. Set custom asyncio loop exception handler to suppress spurious Windows IOCP 10054 callback noise
    loop = asyncio.get_running_loop()

    def handle_asyncio_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        msg = context.get("message", "")
        if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10054, 10053, 121):
            return
        if "An existing connection was forcibly closed by the remote host" in str(exc) or "_call_connection_lost" in msg:
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(handle_asyncio_exception)

    # 1. Initialize Database & Certs & Manifest Assets
    settings.warn_on_insecure_defaults()
    await init_db()
    ensure_certificates()
    generate_all_manifest_assets()

    lan_ip = settings.lan_ip or detect_local_ip()
    logger.info("==================================================================")
    logger.info("Touhou Danmaku Kagura Preservation Server Starting...")
    logger.info("Host LAN IP: %s", lan_ip)
    logger.info("==================================================================")

    # 2. gRPC Server
    grpc_server = create_grpc_server()
    await grpc_server.start()
    logger.info("✅ [Takasho gRPC] Active on port %d", settings.grpc_port)

    # 3. HTTP Certificate Portal & Asset CDN (Port 8080, Plain HTTP)
    http_app = FastAPI(
        title="Danmaku Kagura HTTP Portal & Asset CDN",
        description="Web portal for one-click CA certificate installation and game asset streaming",
        docs_url="/docs",
    )
    http_app.include_router(asset_router)

    http_config = uvicorn.Config(
        http_app,
        host=settings.host,
        port=settings.asset_port,
        log_level="info",
    )
    http_server = uvicorn.Server(http_config)
    logger.info("✅ [HTTP Portal & Cert Delivery] http://%s:%d/", lan_ip, settings.asset_port)

    # 4. LCX Authentication REST API (Port 443 for iOS, Port 9443 for Android)
    lcx_app = create_lcx_app()
    lcx_app.include_router(asset_router)

    ssl_certfile = str(settings.certs_dir / "server.crt")
    ssl_keyfile = str(settings.certs_dir / "server.key")

    lcx_ios_config = uvicorn.Config(
        lcx_app,
        host=settings.host,
        port=settings.lcx_ios_port,
        ssl_certfile=ssl_certfile if Path(ssl_certfile).exists() else None,
        ssl_keyfile=ssl_keyfile if Path(ssl_keyfile).exists() else None,
        log_level="info",
    )
    lcx_ios_server = uvicorn.Server(lcx_ios_config)
    logger.info("✅ [LCX Auth iOS (HTTPS)] https://%s:%d", settings.host, settings.lcx_ios_port)

    lcx_config = uvicorn.Config(
        lcx_app,
        host=settings.host,
        port=settings.lcx_port,
        ssl_certfile=ssl_certfile if Path(ssl_certfile).exists() else None,
        ssl_keyfile=ssl_keyfile if Path(ssl_keyfile).exists() else None,
        log_level="info",
    )
    lcx_server = uvicorn.Server(lcx_config)
    logger.info("✅ [LCX Auth Android (HTTPS)] https://%s:%d", settings.host, settings.lcx_port)

    logger.info("==================================================================")
    logger.info("👉 Step 1: Open a browser on your phone: http://%s:%d/ to install the CA", lan_ip, settings.asset_port)
    logger.info("👉 Step 2: Point the client at %s (LCX https://%s:%d, gRPC %s:%d)",
                settings.public_host or lan_ip, lan_ip, settings.lcx_ios_port, lan_ip, settings.grpc_port)
    logger.info("👉 Step 3: Launch Danmaku Kagura app!")
    logger.info("==================================================================")

    try:
        await asyncio.gather(
            http_server.serve(),
            lcx_ios_server.serve(),
            lcx_server.serve(),
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutting down servers...")
    finally:
        try:
            await asyncio.wait_for(grpc_server.stop(grace=1.0), timeout=2.0)
        except Exception:
            pass
        logger.info("Backend servers cleanly stopped.")


def main() -> None:
    try:
        asyncio.run(run_servers())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
