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

        res = await client.post("/identity/link-state")
        assert res.status_code == 200
        assert res.json()["hasAnyLink"] is True

        res = await client.post("/identity/sign-out")
        assert res.status_code == 204
