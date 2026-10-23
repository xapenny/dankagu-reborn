import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dankagu.assets.server import game_router, ops_router
from dankagu.config import settings
from dankagu.core.certs import generate_certificates

#: Routes only the operator uses. The game client never calls any of them.
OPS_PATHS = ["/", "/ca.crt", "/cert", "/server.crt", "/status"]

#: Routes the game does call, including the ToS/privacy text the in-game
#: WebView pulls through ``/static``.
GAME_PATHS = ["/assets/announcements/probe.png", "/static/tos", "/static/pp"]


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8080"
    ) as http_client:
        return await http_client.get(path)


def _app_with(*routers: object) -> FastAPI:
    app = FastAPI()
    for router in routers:
        app.include_router(router)  # type: ignore[arg-type]
    return app


@pytest.fixture(autouse=True)
def setup_certs() -> None:
    """Ensure certificates exist for portal testing."""
    if not (settings.certs_dir / "ca.crt").exists():
        generate_certificates(settings.certs_dir)


@pytest.mark.asyncio
async def test_portal_html_index() -> None:
    app = _app_with(ops_router)

    res = await _get(app, "/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Danmaku Kagura" in res.text
    assert "Download DanKagu Root CA" in res.text
    assert "/ca.crt" in res.text


@pytest.mark.asyncio
async def test_portal_ca_certificate_download() -> None:
    app = _app_with(ops_router)

    res = await _get(app, "/ca.crt")
    assert res.status_code == 200
    assert "application/x-x509-ca-cert" in res.headers["content-type"]
    assert b"BEGIN CERTIFICATE" in res.content


@pytest.mark.asyncio
async def test_portal_server_certificate_download() -> None:
    app = _app_with(ops_router)

    res = await _get(app, "/server.crt")
    assert res.status_code == 200
    assert "application/x-x509-ca-cert" in res.headers["content-type"]
    assert b"BEGIN CERTIFICATE" in res.content


@pytest.mark.asyncio
async def test_portal_status_endpoint() -> None:
    app = _app_with(ops_router)

    res = await _get(app, "/status")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "online"
    assert "lan_ip" in data
    assert data["ports"]["http_portal_cdn"] == 8080
    assert data["certificates"]["ca_present"] is True


# --------------------------------------------------------------------------- #
# The operator routes belong to the plain-HTTP listener only
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_portal_app_serves_both_audiences() -> None:
    """The plain-HTTP listener is the one place the operator routes live."""
    from dankagu.main import create_portal_app

    app = create_portal_app()
    for path in OPS_PATHS:
        assert (await _get(app, path)).status_code == 200, f"{path} missing on the portal app"
    for path in GAME_PATHS:
        assert (await _get(app, path)).status_code == 200, f"{path} missing on the portal app"


@pytest.mark.asyncio
async def test_public_https_app_has_no_operator_routes() -> None:
    """443/9443 must not expose the portal, the certificates or /status."""
    from dankagu.main import create_public_https_app

    app = create_public_https_app()
    for path in OPS_PATHS:
        res = await _get(app, path)
        assert res.status_code == 404, f"public HTTPS app exposed {path}"


@pytest.mark.asyncio
async def test_game_routes_are_served() -> None:
    """The client keeps fetching assets, ToS/PP text and photos."""
    app = _app_with(game_router)
    for path in GAME_PATHS:
        assert (await _get(app, path)).status_code == 200, f"{path} is not served"


@pytest.mark.asyncio
async def test_public_https_app_serves_no_api_docs() -> None:
    """The OpenAPI schema must not be published on the public HTTPS ports."""
    from dankagu.main import create_public_https_app

    app = create_public_https_app()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await _get(app, path)).status_code == 404, f"public HTTPS app served {path}"
