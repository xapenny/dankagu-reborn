import pytest
from httpx import ASGITransport, AsyncClient

from dankagu.lcx.app import create_lcx_app


@pytest.mark.asyncio
async def test_lcx_environment() -> None:
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        response = await client.post("/environment")
        assert response.status_code == 200
        data = response.json()
        assert data["applicationId"] == "danmakujp4-v1"
        assert data["isProd"] is True


@pytest.mark.asyncio
async def test_lcx_user_access_token() -> None:
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        response = await client.post("/auth/user-access-token")
        assert response.status_code == 200
        data = response.json()
        assert "userId" in data
        assert "accessToken" in data
        assert "signInSessionId" in data


@pytest.mark.asyncio
async def test_lcx_identity_transfer() -> None:
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        response = await client.post("/identity/transfer/guest_user_1/execute")
        assert response.status_code == 200
        data = response.json()
        assert "accessToken" in data
        assert "lcxUserIdToken" in data
        assert "user" in data
        assert data["user"]["store"] == "GUEST"
        assert "signInSessionToken" in data


@pytest.mark.asyncio
async def test_lcx_auxiliary_endpoints() -> None:
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        res = await client.get("/gcp/analytics/config")
        assert res.status_code == 200
        assert res.json()["isPubsubEnabled"] is False

        res = await client.get("/identity/active-published-store")
        assert res.status_code == 200
        assert res.json() == {"store": "GUEST"}

        res = await client.post("/gcp/identity/active-published-store")
        assert res.status_code == 200
        assert res.json() == {"store": "GUEST"}

        res = await client.post("/identity/link-state")
        assert res.status_code == 200
        assert res.json()["hasAnyLink"] is False

        res = await client.post("/identity/sign-out")
        assert res.status_code == 204


@pytest.mark.asyncio
async def test_lcx_iap_endpoints() -> None:
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        res = await client.get("/iap/product?store=GUEST")
        assert res.status_code == 200
        assert isinstance(res.json(), list)

        res = await client.get("/iap/purchase-summary")
        assert res.status_code == 200
        assert res.json()["count"] == 0

        res = await client.post("/iap/purchase/retry-incomplete-purchases")
        assert res.status_code == 200
        assert res.json() == {"success": [], "error": []}
