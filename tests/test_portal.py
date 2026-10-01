import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dankagu.assets.server import router as asset_router
from dankagu.config import settings
from dankagu.core.certs import generate_certificates


@pytest.fixture(autouse=True)
def setup_certs() -> None:
    """Ensure certificates exist for portal testing."""
    if not (settings.certs_dir / "ca.crt").exists():
        generate_certificates(settings.certs_dir)


@pytest.mark.asyncio
async def test_portal_html_index() -> None:
    app = FastAPI()
    app.include_router(asset_router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        res = await client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "Danmaku Kagura" in res.text
        assert "Download DanKagu Root CA" in res.text
        assert "/ca.crt" in res.text


@pytest.mark.asyncio
async def test_portal_ca_certificate_download() -> None:
    app = FastAPI()
    app.include_router(asset_router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        res = await client.get("/ca.crt")
        assert res.status_code == 200
        assert "application/x-x509-ca-cert" in res.headers["content-type"]
        assert b"BEGIN CERTIFICATE" in res.content


@pytest.mark.asyncio
async def test_portal_server_certificate_download() -> None:
    app = FastAPI()
    app.include_router(asset_router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        res = await client.get("/server.crt")
        assert res.status_code == 200
        assert "application/x-x509-ca-cert" in res.headers["content-type"]
        assert b"BEGIN CERTIFICATE" in res.content


@pytest.mark.asyncio
async def test_portal_status_endpoint() -> None:
    app = FastAPI()
    app.include_router(asset_router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        res = await client.get("/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "online"
        assert "lan_ip" in data
        assert data["ports"]["http_portal_cdn"] == 8080
        assert data["certificates"]["ca_present"] is True
