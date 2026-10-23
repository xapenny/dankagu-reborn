"""Integration tests for Phase 1: Session context tracking and multi-tenant isolation.

Verifies:
1. Multi-tenant LCX registration: Two different devices obtain distinct user identities.
2. System.AuthorizeV3: Issues unique, persistent session tokens and allocates distinct players.
3. gRPC interceptor & context: Correctly extracts x-takasho-session-token and resolves player_id.
4. PlayerStorage multi-tenant isolation: Two players mutating the same key do NOT experience crosstalk.
5. GetOtherPlayerEntriesV2: Explicit cross-player entry inspection works cleanly.
"""

import uuid
from typing import Any

import grpc
import pytest
from httpx import ASGITransport, AsyncClient
from player_factory import ensure_player

from dankagu.core.session import (
    create_session,
    get_current_player_id,
    reset_current_player,
    resolve_session_player_id,
    set_current_player,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    player_storage_pb2,
    system_pb2,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_storage.v2_pb2 import (
    Criterion,
    Entry,
)
from dankagu.grpc.interceptors import TakashoAsyncInterceptor
from dankagu.lcx.app import create_lcx_app
from dankagu.services.player_storage import PlayerStorageService
from dankagu.services.system import SystemService


class MockContext:
    def __init__(self, metadata: list[tuple[str, str]] | None = None) -> None:
        self._metadata = metadata or []

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self._metadata

    async def send_initial_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        pass

    def set_trailing_metadata(self, metadata: tuple[tuple[str, str], ...]) -> None:
        pass

    async def abort(self, code: Any, details: str = "") -> None:
        raise grpc.RpcError(f"Aborted: {code} - {details}")


@pytest.mark.asyncio
async def test_multi_device_registration_issues_distinct_players() -> None:
    """Two different devices registering via LCX receive distinct user identities."""
    app = create_lcx_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://danmakujp4-v1.lcx.tokyo"
    ) as client:
        res_a = await client.post(
            "/identity/sign-in",
            json={"storeAccountIdHash": f"device-hash-{uuid.uuid4()}"},
        )
        assert res_a.status_code == 200
        user_a = res_a.json()["user"]

        res_b = await client.post(
            "/identity/sign-in",
            json={"storeAccountIdHash": f"device-hash-{uuid.uuid4()}"},
        )
        assert res_b.status_code == 200
        user_b = res_b.json()["user"]

        assert user_a["id"] != user_b["id"]


@pytest.mark.asyncio
async def test_authorize_v3_session_persistence_and_multi_tenant() -> None:
    """AuthorizeV3 generates distinct players and persistent session tokens."""
    system_service = SystemService()
    context = MockContext()

    device_a = f"dev-acc-{uuid.uuid4()}"
    device_b = f"dev-acc-{uuid.uuid4()}"

    # Authorize Player A
    req_a = system_pb2.SystemAuthorizeV3.Request(device_account=device_a)
    resp_a = await system_service.AuthorizeV3(req_a, context)  # type: ignore[arg-type]

    # Authorize Player B
    req_b = system_pb2.SystemAuthorizeV3.Request(device_account=device_b)
    resp_b = await system_service.AuthorizeV3(req_b, context)  # type: ignore[arg-type]

    assert resp_a.player_id != resp_b.player_id
    assert resp_a.session_token != resp_b.session_token

    # Verify session tokens resolve to correct players
    assert await resolve_session_player_id(resp_a.session_token) == resp_a.player_id
    assert await resolve_session_player_id(resp_b.session_token) == resp_b.player_id

    # Non-existent session token resolves to None
    assert await resolve_session_player_id(str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_player_storage_complete_isolation_between_tenants() -> None:
    """Two concurrent players saving the same key must never leak or overwrite each other's state."""
    player_storage = PlayerStorageService()
    context = MockContext()

    pid_a = f"player-tenant-A-{uuid.uuid4()}"
    pid_b = f"player-tenant-B-{uuid.uuid4()}"

    key = "DeckStatus--ActiveSlot"
    val_a = b"\xaa\xaa\xaa\xaa"
    val_b = b"\xbb\xbb\xbb\xbb"

    # Player A sets key
    tokens_a = set_current_player(await ensure_player(pid_a))
    try:
        set_req_a = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
            entries=[Entry(key=key, value=val_a)],
            next_revision="rev-a-1",
        )
        set_res_a = await player_storage.SetEntriesV2(set_req_a, context)  # type: ignore[arg-type]
        assert set_res_a.entries[0].player_id == pid_a
    finally:
        reset_current_player(tokens_a)

    # Player B sets the SAME key with different value
    tokens_b = set_current_player(await ensure_player(pid_b))
    try:
        set_req_b = player_storage_pb2.PlayerStorageSetEntriesV2.Request(
            entries=[Entry(key=key, value=val_b)],
            next_revision="rev-b-1",
        )
        set_res_b = await player_storage.SetEntriesV2(set_req_b, context)  # type: ignore[arg-type]
        assert set_res_b.entries[0].player_id == pid_b
    finally:
        reset_current_player(tokens_b)

    # Player A reads the key back -> must see val_a, NOT val_b
    tokens_a = set_current_player(await ensure_player(pid_a))
    try:
        get_req_a = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
            criteria=[Criterion(key=key, matching_type=Criterion.MatchingType.EXACT)]
        )
        get_res_a = await player_storage.GetEntriesV2(get_req_a, context)  # type: ignore[arg-type]
        assert len(get_res_a.entries) == 1
        assert get_res_a.entries[0].player_id == pid_a
        assert get_res_a.entries[0].value == val_a
    finally:
        reset_current_player(tokens_a)

    # Player B reads the key back -> must see val_b, NOT val_a
    tokens_b = set_current_player(await ensure_player(pid_b))
    try:
        get_req_b = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
            criteria=[Criterion(key=key, matching_type=Criterion.MatchingType.EXACT)]
        )
        get_res_b = await player_storage.GetEntriesV2(get_req_b, context)  # type: ignore[arg-type]
        assert len(get_res_b.entries) == 1
        assert get_res_b.entries[0].player_id == pid_b
        assert get_res_b.entries[0].value == val_b
    finally:
        reset_current_player(tokens_b)


@pytest.mark.asyncio
async def test_get_other_player_entries_v2() -> None:
    """GetOtherPlayerEntriesV2 allows inspecting another player's public storage entries."""
    player_storage = PlayerStorageService()
    context = MockContext()

    target_pid = f"target-player-{uuid.uuid4()}"
    key = "PublicProfile--1"
    val = b"\x10\x20\x30\x40"

    # Seed target player's entry
    tokens = set_current_player(await ensure_player(target_pid))
    try:
        await player_storage.SetEntriesV2(
            player_storage_pb2.PlayerStorageSetEntriesV2.Request(
                entries=[Entry(key=key, value=val)],
                next_revision="rev-1",
            ),
            context,  # type: ignore[arg-type]
        )
    finally:
        reset_current_player(tokens)

    # Other player queries target_pid's entries
    observer_pid = f"observer-player-{uuid.uuid4()}"
    tokens_obs = set_current_player(await ensure_player(observer_pid))
    try:
        other_req = player_storage_pb2.PlayerStorageGetOtherPlayerEntriesV2.Request(
            player_ids=[target_pid],
            criteria=[
                Criterion(key="PublicProfile--", matching_type=Criterion.MatchingType.FORWARD)
            ],
        )
        other_res = await player_storage.GetOtherPlayerEntriesV2(other_req, context)  # type: ignore[arg-type]
        assert len(other_res.entries) == 1
        assert other_res.entries[0].player_id == target_pid
        assert other_res.entries[0].key == key
        assert other_res.entries[0].value == val
    finally:
        reset_current_player(tokens_obs)


@pytest.mark.asyncio
async def test_grpc_interceptor_session_token_binding() -> None:
    """The TakashoAsyncInterceptor binds x-takasho-session-token metadata to current_player_id."""
    interceptor = TakashoAsyncInterceptor()

    import time

    from dankagu.core.database import get_sessionmaker
    from dankagu.models.player import Player

    test_pid = str(uuid.uuid4())
    test_uid = str(uuid.uuid4())
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        session.add(Player(id=test_pid, user_id=test_uid, registered_at=int(time.time())))
        await session.commit()

    session_token = await create_session(player_id=test_pid, user_id=test_uid)

    captured_pid: str | None = None

    async def dummy_method_handler(request: Any, context: grpc.aio.ServicerContext) -> str:
        nonlocal captured_pid
        captured_pid = get_current_player_id()
        return "ok"

    handler = grpc.unary_unary_rpc_method_handler(dummy_method_handler)

    class DummyCallDetails:
        method = "/takasho.test/Dummy"

    async def continuation(details: grpc.HandlerCallDetails) -> Any:
        return handler

    intercepted = await interceptor.intercept_service(continuation, DummyCallDetails())  # type: ignore[arg-type]

    # Invoke with session token metadata
    context_with_token = MockContext(metadata=[("x-takasho-session-token", session_token)])
    await intercepted.unary_unary(b"", context_with_token)

    assert captured_pid == test_pid

    # Outside handler, context is reset
    assert get_current_player_id() is None


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected_by_interceptor() -> None:
    """An authenticated RPC requested without a session token must be aborted with UNAUTHENTICATED."""
    interceptor = TakashoAsyncInterceptor()

    async def dummy_method_handler(request: Any, context: grpc.aio.ServicerContext) -> str:
        return "ok"

    handler = grpc.unary_unary_rpc_method_handler(dummy_method_handler)

    class DummyCallDetails:
        method = "/takasho.schema.common_featureset.player_api.PlayerStorage/GetEntriesV2"

    async def continuation(details: grpc.HandlerCallDetails) -> Any:
        return handler

    intercepted = await interceptor.intercept_service(continuation, DummyCallDetails())  # type: ignore[arg-type]

    # Invoke WITHOUT session token metadata -> must raise RpcError
    context_no_token = MockContext(metadata=[])
    with pytest.raises(grpc.RpcError) as exc_info:
        await intercepted.unary_unary(b"", context_no_token)

    assert "StatusCode.UNAUTHENTICATED" in str(exc_info.value)


@pytest.mark.asyncio
async def test_invalid_session_token_rejected_by_interceptor() -> None:
    """An authenticated RPC requested with a non-existent or expired session token must be aborted."""
    interceptor = TakashoAsyncInterceptor()

    async def dummy_method_handler(request: Any, context: grpc.aio.ServicerContext) -> str:
        return "ok"

    handler = grpc.unary_unary_rpc_method_handler(dummy_method_handler)

    class DummyCallDetails:
        method = "/takasho.schema.common_featureset.player_api.Wallet/GetBalancesV2"

    async def continuation(details: grpc.HandlerCallDetails) -> Any:
        return handler

    intercepted = await interceptor.intercept_service(continuation, DummyCallDetails())  # type: ignore[arg-type]

    # Invoke with forged/deleted session token
    fake_token = str(uuid.uuid4())
    context_bad_token = MockContext(metadata=[("x-takasho-session-token", fake_token)])
    with pytest.raises(grpc.RpcError) as exc_info:
        await intercepted.unary_unary(b"", context_bad_token)

    assert "StatusCode.UNAUTHENTICATED" in str(exc_info.value)


@pytest.mark.asyncio
async def test_player_storage_without_session_aborts() -> None:
    """Direct invocation of PlayerStorageService without an active session context must abort."""
    service = PlayerStorageService()
    context = MockContext()

    req = player_storage_pb2.PlayerStorageGetEntriesV2.Request(
        criteria=[Criterion(key="CardStatus--", matching_type=Criterion.MatchingType.FORWARD)]
    )
    with pytest.raises(grpc.RpcError) as exc_info:
        await service.GetEntriesV2(req, context)  # type: ignore[arg-type]

    assert "StatusCode.UNAUTHENTICATED" in str(exc_info.value)
