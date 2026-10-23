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
from dankagu.assets.server import game_router, ops_router
from dankagu.config import settings
from dankagu.core.certs import generate_certificates
from dankagu.core.database import seed_runtime_data
from dankagu.core.net import detect_local_ip
from dankagu.db.migrate import run_migrations
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
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    handlers=[console_handler, file_handler],
)
logger = logging.getLogger("dankagu.startup")
# Startup / lifecycle messages (banner, listening ports, cert generation) are
# always shown, even when DANKAGU_LOG_LEVEL is WARNING or ERROR.
logger.setLevel(logging.INFO)


def ensure_certificates() -> None:
    cert_path = settings.certs_dir / "server.crt"
    key_path = settings.certs_dir / "server.key"
    if not cert_path.exists() or not key_path.exists():
        logger.info("TLS certificates not found. Generating fresh certificates...")
        generate_certificates(settings.certs_dir)
        return

    missing = _missing_san_names(cert_path)
    if missing:
        logger.info(
            "TLS certificate SAN is missing %s. Regenerating leaf (Root CA reused)...",
            ", ".join(missing),
        )
        generate_certificates(settings.certs_dir)


def _missing_san_names(cert_path: Path) -> list[str]:
    """Return configured DNS names not covered by the leaf certificate's SAN."""
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:
        return ["<unreadable SAN>"]
    present = {n.lower() for n in san.get_values_for_type(x509.DNSName)}

    def covered(name: str) -> bool:
        name = name.lower()
        if name in present:
            return True
        _, _, rest = name.partition(".")
        return bool(rest) and f"*.{rest}" in present

    return [n for n in settings.cert_dns_names if not covered(n)]


def create_portal_app() -> FastAPI:
    """Build the plain-HTTP portal & asset CDN application.

    The only listener that serves the operator routes (setup portal, CA/server
    certificates, ``/status``) alongside the game's asset routes. Kept plaintext
    because a device has to reach it *before* it trusts this server's
    certificate.
    """
    http_app = FastAPI(
        title="Danmaku Kagura HTTP Portal & Asset CDN",
        description="Web portal for one-click CA certificate installation and game asset streaming",
        docs_url="/docs",
    )
    http_app.include_router(game_router)
    http_app.include_router(ops_router)
    return http_app


def create_public_https_app() -> FastAPI:
    """Build the app served on the public HTTPS listeners (ports 443 / 9443).

    LCX authentication plus the game's asset routes, because the client fetches
    AssetBundles over HTTPS once ``DANKAGU_PUBLIC_HOST`` is set. The operator
    routes are deliberately absent: they belong to the plain-HTTP listener.
    """
    lcx_app = create_lcx_app()
    lcx_app.include_router(game_router)
    return lcx_app


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
        if (
            "An existing connection was forcibly closed by the remote host" in str(exc)
            or "_call_connection_lost" in msg
        ):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(handle_asyncio_exception)

    # 1. Initialize Database & Certs & Manifest Assets
    settings.warn_on_insecure_defaults()
    # Schema first (versioned, advisory-locked, legacy databases are stamped with
    # the baseline revision instead of rebuilt), then idempotent data seeding.
    # A migration failure aborts startup on purpose: serving traffic against a
    # half-migrated schema corrupts state in ways that are far harder to repair.
    if settings.db_auto_migrate:
        migration = await run_migrations()
        logger.info("🗄️  Schema migration: %s", migration.describe())
    else:
        logger.info(
            "⏭️  DANKAGU_DB_AUTO_MIGRATE=false - schema migration skipped "
            "(apply it with: python -m dankagu.db.migrate --upgrade)"
        )
    await seed_runtime_data()
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

    # 3. HTTP Certificate Portal & Asset CDN (Port 8080, Plain HTTP).
    #    Optional: DANKAGU_ASSET_SERVER_ENABLED=false leaves the port unbound.
    http_servers: list[uvicorn.Server] = []
    if settings.asset_server_enabled:
        http_config = uvicorn.Config(
            create_portal_app(),
            host=settings.host,
            port=settings.asset_port,
            log_level=settings.log_level.lower(),
        )
        http_servers.append(uvicorn.Server(http_config))
        logger.info("✅ [HTTP Portal & Cert Delivery] http://%s:%d/", lan_ip, settings.asset_port)
    else:
        logger.info(
            "⏸️  [HTTP Portal & Cert Delivery] Disabled via DANKAGU_ASSET_SERVER_ENABLED=false "
            "- port %d is not bound.",
            settings.asset_port,
        )
        logger.warning(
            "   Assets remain available over HTTPS on port %d; the CA must be installed on "
            "each device by other means, since plain-HTTP delivery is off.",
            settings.lcx_port,
        )

    # 4. LCX Authentication REST API (Port 443 for iOS, Port 9443 for Android)
    lcx_app = create_public_https_app()

    ssl_certfile = str(settings.certs_dir / "server.crt")
    ssl_keyfile = str(settings.certs_dir / "server.key")

    lcx_ios_config = uvicorn.Config(
        lcx_app,
        host=settings.host,
        port=settings.lcx_ios_port,
        ssl_certfile=ssl_certfile if Path(ssl_certfile).exists() else None,
        ssl_keyfile=ssl_keyfile if Path(ssl_keyfile).exists() else None,
        log_level=settings.log_level.lower(),
        # The client keeps its TLS connections open for the whole session and reuses
        # them. Uvicorn's default 5 s keep-alive closes an idle connection, and the
        # client then waits on the dead socket - observed as multi-minute freezes at
        # startup (two sockets idle ~30 s each, then CLOSE-WAIT with the server's TLS
        # close_notify still unread). Keep them open far longer than any client idle.
        timeout_keep_alive=600,
    )
    lcx_ios_server = uvicorn.Server(lcx_ios_config)
    logger.info("✅ [LCX Auth iOS (HTTPS)] https://%s:%d", settings.host, settings.lcx_ios_port)

    lcx_config = uvicorn.Config(
        lcx_app,
        host=settings.host,
        port=settings.lcx_port,
        ssl_certfile=ssl_certfile if Path(ssl_certfile).exists() else None,
        ssl_keyfile=ssl_keyfile if Path(ssl_keyfile).exists() else None,
        log_level=settings.log_level.lower(),
        timeout_keep_alive=600,
    )
    lcx_server = uvicorn.Server(lcx_config)
    logger.info("✅ [LCX Auth Android (HTTPS)] https://%s:%d", settings.host, settings.lcx_port)

    logger.info("==================================================================")
    if settings.asset_server_enabled:
        logger.info(
            "👉 Step 1: Open a browser on your phone: http://%s:%d/ to install the CA",
            lan_ip,
            settings.asset_port,
        )
    else:
        logger.info("👉 Step 1: Install certs/ca.crt on your device manually (portal disabled)")
    logger.info(
        "👉 Step 2: Point the client at %s (LCX https://%s:%d, gRPC %s:%d)",
        settings.public_host or lan_ip,
        lan_ip,
        settings.lcx_ios_port,
        lan_ip,
        settings.grpc_port,
    )
    logger.info("👉 Step 3: Launch Danmaku Kagura app!")
    logger.info("==================================================================")

    try:
        await asyncio.gather(
            *(server.serve() for server in http_servers),
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
