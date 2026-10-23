"""Identity and session invariants: which device may reach which account."""

import time
import uuid

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from dankagu.config import settings
from dankagu.core.database import get_sessionmaker
from dankagu.core.session import invalidate_session, resolve_session_player_id
from dankagu.lcx.app import create_lcx_app
from dankagu.lcx.tokens import generate_lcx_tokens
from dankagu.models.player import Player

_BASE = "https://danmakujp4-v1.lcx.tokyo"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_lcx_app()), base_url=_BASE)


async def _load_player(user_id: str) -> Player | None:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(Player).where(Player.user_id == user_id))
        ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_new_device_registers_a_fresh_unonboarded_account() -> None:
    """A brand new device must land on an account that still needs onboarding."""
    async with _client() as client:
        res = await client.post(
            "/identity/sign-in", headers={"deviceAccount": f"fresh-{uuid.uuid4()}"}
        )
    assert res.status_code == 200

    player = await _load_player(res.json()["user"]["id"])
    assert player is not None
    # Empty nickname / ToS is what makes the client show the registration flow.
    assert player.nickname == ""
    assert player.consented_tos_version == ""


@pytest.mark.asyncio
async def test_token_presented_from_another_device_is_rejected() -> None:
    """Device B replaying device A's token must not reach A's account."""
    device_a = f"device-a-{uuid.uuid4()}"
    device_b = f"device-b-{uuid.uuid4()}"

    async with _client() as client:
        sign_in = await client.post("/identity/sign-in", headers={"deviceAccount": device_a})
        assert sign_in.status_code == 200
        user_a = sign_in.json()["user"]["id"]
        token = sign_in.json()["signInSessionToken"]

        # Same token, same device: allowed.
        same_device = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device_a},
            json={"signInSessionToken": token},
        )
        assert same_device.status_code == 200
        assert same_device.json()["userId"] == user_a

        # Same token, different device: must not hand over device A's account.
        other_device = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device_b},
            json={"signInSessionToken": token},
        )
    assert other_device.status_code == 401
    assert other_device.json()["code"] == "LCX_INVALID_SESSION_DEVICE"


@pytest.mark.asyncio
async def test_forged_token_cannot_claim_an_existing_account() -> None:
    """Without a valid signature a token must not be able to name a subject."""
    async with _client() as client:
        sign_in = await client.post(
            "/identity/sign-in", headers={"deviceAccount": f"victim-{uuid.uuid4()}"}
        )
        victim_user_id = sign_in.json()["user"]["id"]

        forged = jwt.encode(
            {"kind": "SIGN_IN_SESSION_TOKEN", "data": {"userId": victim_user_id}},
            "not-the-server-secret-but-long-enough",
            algorithm="HS256",
        )
        res = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": f"attacker-{uuid.uuid4()}"},
            json={"signInSessionToken": forged},
        )
    assert res.status_code == 200
    # It resolves to a brand new account, never to the victim's identity.
    assert res.json()["userId"] != victim_user_id


async def _onboarded_device(device: str) -> str:
    """Create an account on ``device`` and mark it onboarded; return its user id."""
    async with _client() as client:
        first = await client.post("/identity/sign-in", headers={"deviceAccount": device})
        assert first.status_code == 200
        user_id = first.json()["user"]["id"]

    # What SetPreferenceV1 does when the client finishes the onboarding questions.
    async with get_sessionmaker()() as session:
        player = (
            await session.execute(select(Player).where(Player.user_id == user_id))
        ).scalar_one()
        player.nickname = "onboarded"
        player.consented_tos_version = "v1"
        await session.commit()
    return user_id


@pytest.mark.asyncio
async def test_a_fresh_install_without_a_token_never_claims_an_onboarded_account() -> None:
    """A tokenless request (fresh install) must never enter an already-onboarded account.

    Android builds that share a hardware identifier across devices would otherwise hand
    the first player's onboarded account to every subsequent fresh install.
    """
    device = f"shared-hw-{uuid.uuid4()}"
    user_id = await _onboarded_device(device)

    async with _client() as client:
        fresh = await client.post("/identity/sign-in", headers={"deviceAccount": device})
    assert fresh.status_code == 200
    fresh_user_id = fresh.json()["user"]["id"]
    assert fresh_user_id != user_id

    # And the fresh install's new account solidifies across restarts via its issued token.
    fresh_token = fresh.json()["signInSessionToken"]
    async with _client() as client:
        restarted = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device},
            json={"signInSessionToken": fresh_token},
        )
    assert restarted.status_code == 200
    assert restarted.json()["userId"] == fresh_user_id


@pytest.mark.asyncio
async def test_an_unbound_identity_does_not_move_between_devices_and_solidifies() -> None:
    """Two phones, one LCX identity, different device values: this is the operator's log.

    When Device B presents an unbound token naming Device A's account, Device B receives
    its own new account AND keeps that exact account across subsequent restarts (even
    after completing onboarding), rather than creating a new account on every launch.
    """
    device_a = f"phone-a-{uuid.uuid4()}"
    device_b = f"phone-b-{uuid.uuid4()}"

    async with _client() as client:
        first = await client.post("/identity/sign-in", headers={"deviceAccount": device_a})
        assert first.status_code == 200
        user_a = first.json()["user"]["id"]
        player_a = await _load_player(user_a)
        assert player_a is not None

        # An identity with no device claim, exactly like the ones the log showed.
        unbound = generate_lcx_tokens(user_a, player_a.registered_at)["signInSessionToken"]
        launch_1 = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device_b},
            json={"signInSessionToken": unbound},
        )
        assert launch_1.status_code == 200
        user_b = launch_1.json()["userId"]
        assert user_b != user_a

        # Mark Device B's new account as onboarded (nickname + ToS set).
        async with get_sessionmaker()() as session:
            player_b = (
                await session.execute(select(Player).where(Player.user_id == user_b))
            ).scalar_one()
            player_b.nickname = "SolidifiedPlayerB"
            player_b.consented_tos_version = "v2.1.0"
            await session.commit()

        # Launch 2 & Launch 3 on Device B (still presenting the same signInSessionToken on disk):
        # must land on the exact same solidified account user_b!
        launch_2 = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device_b},
            json={"signInSessionToken": unbound},
        )
        launch_3 = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device_b},
            json={"signInSessionToken": unbound},
        )
        assert launch_2.status_code == 200 and launch_3.status_code == 200
        assert launch_2.json()["userId"] == user_b
        assert launch_3.json()["userId"] == user_b


@pytest.mark.asyncio
async def test_an_abandoned_registration_is_reused() -> None:
    """The hint is still honoured while the account has not been onboarded."""
    device = f"abandoned-{uuid.uuid4()}"

    async with _client() as client:
        first = await client.post("/identity/sign-in", headers={"deviceAccount": device})
        second = await client.post("/identity/sign-in", headers={"deviceAccount": device})

    assert first.status_code == 200 and second.status_code == 200
    # Nothing was ever entered for it, so reusing it costs nobody anything.
    assert first.json()["user"]["id"] == second.json()["user"]["id"]


@pytest.mark.asyncio
async def test_sign_out_revokes_the_session_token() -> None:
    from dankagu.core.session import create_session

    async with _client() as client:
        sign_in = await client.post(
            "/identity/sign-in", headers={"deviceAccount": f"signout-{uuid.uuid4()}"}
        )
        assert sign_in.status_code == 200
        player = await _load_player(sign_in.json()["user"]["id"])
        assert player is not None

        session_token = await create_session(player.id, player.user_id)
        assert await resolve_session_player_id(session_token) == player.id

        res = await client.post(
            "/identity/sign-out", headers={"Authorization": f"Bearer {session_token}"}
        )
        assert res.status_code == 204

    assert await resolve_session_player_id(session_token) is None
    # Cleanup: make sure a stale token cannot linger even if the test fails early.
    await invalidate_session(session_token)


@pytest.mark.asyncio
async def test_pre_binding_token_is_accepted_on_its_own_device() -> None:
    """An existing install must not be locked out while it upgrades its token.

    The token records no device, but the account it names already belongs to this
    device, so the claim is good -- and the tokens handed back carry the binding.
    """
    device = f"legacy-own-{uuid.uuid4()}"
    async with _client() as client:
        sign_in = await client.post("/identity/sign-in", headers={"deviceAccount": device}, json={})
        assert sign_in.status_code == 200
        user_id = sign_in.json()["user"]["id"]

        legacy_token = jwt.encode(
            {
                "kind": "SIGN_IN_SESSION_TOKEN",
                "data": {"store": "GUEST", "sessionId": str(uuid.uuid4()), "userId": user_id},
            },
            settings.require_jwt_secret(),
            algorithm="HS256",
        )
        refreshed = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device},
            json={"signInSessionToken": legacy_token},
        )

    assert refreshed.status_code == 200
    assert refreshed.json()["userId"] == user_id

    # The replacement access token carries the device binding, so the strict rule
    # applies from now on.
    replacement = refreshed.json()["accessToken"]
    payload = jwt.decode(replacement, settings.require_jwt_secret(), algorithms=["HS256"])
    assert payload["data"]["session"]["deviceHash"] == device


@pytest.mark.asyncio
async def test_pre_binding_token_from_another_device_signs_in_as_a_new_player() -> None:
    """An unbound token proves nothing, so the stranger gets their own account.

    The account the token names must never be handed over -- and it is not.
    """
    owner_device = f"legacy-owner-{uuid.uuid4()}"
    async with _client() as client:
        sign_in = await client.post(
            "/identity/sign-in", headers={"deviceAccount": owner_device}, json={}
        )
        user_id = sign_in.json()["user"]["id"]

        legacy_token = jwt.encode(
            {
                "kind": "SIGN_IN_SESSION_TOKEN",
                "data": {"store": "GUEST", "sessionId": str(uuid.uuid4()), "userId": user_id},
            },
            settings.require_jwt_secret(),
            algorithm="HS256",
        )
        res = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": f"thief-{uuid.uuid4()}"},
            json={"signInSessionToken": legacy_token},
        )
        assert res.status_code == 200, res.text
        assert res.json()["userId"] != user_id, "the stranger must not get that account"

        # ... and the owner is unaffected on their own device
        owner = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": owner_device},
            json={"signInSessionToken": sign_in.json()["signInSessionToken"]},
        )
        assert owner.status_code == 200
        assert owner.json()["userId"] == user_id


@pytest.mark.asyncio
async def test_a_token_for_an_unknown_user_signs_the_device_in_as_a_new_player() -> None:
    """A token naming an account that does not exist is not a credential either."""
    legacy_payload = {
        "kind": "SIGN_IN_SESSION_TOKEN",
        "data": {"store": "GUEST", "userId": "legacy-user", "sessionId": str(uuid.uuid4())},
    }
    legacy_token = jwt.encode(legacy_payload, settings.require_jwt_secret(), algorithm="HS256")

    async with _client() as client:
        res = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": f"legacy-device-{uuid.uuid4()}"},
            json={"signInSessionToken": legacy_token},
        )
    assert res.status_code == 200, res.text
    assert res.json()["userId"] != "legacy-user"
    assert res.json()["accessToken"]


@pytest.mark.asyncio
async def test_expired_token_still_returns_the_player_to_their_account() -> None:
    """A long break must not cost an account: same device, stale token, back in.

    The claim is expired, so it proves nothing on its own; the account it names does
    belong to this device, and that is what is checked.
    """
    device = f"returning-{uuid.uuid4()}"
    async with _client() as client:
        sign_in = await client.post("/identity/sign-in", headers={"deviceAccount": device}, json={})
        assert sign_in.status_code == 200
        user_id = sign_in.json()["user"]["id"]

        expired = jwt.encode(
            {
                "kind": "SIGN_IN_SESSION_TOKEN",
                "exp": int(time.time()) - 86400,
                "data": {
                    "store": "GUEST",
                    "sessionId": str(uuid.uuid4()),
                    "userId": user_id,
                    "deviceHash": device,
                },
            },
            settings.require_jwt_secret(),
            algorithm="HS256",
        )
        refreshed = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": device},
            json={"signInSessionToken": expired},
        )

    assert refreshed.status_code == 200
    assert refreshed.json()["userId"] == user_id


@pytest.mark.asyncio
async def test_an_expired_token_from_another_device_signs_in_as_a_new_player() -> None:
    """A leaked token must stop reaching the account once its lifetime runs out.

    Bound to a device, the mismatch is reported; unbound, the device gets a new player.
    """
    owner_device = f"expired-owner-{uuid.uuid4()}"
    async with _client() as client:
        sign_in = await client.post(
            "/identity/sign-in", headers={"deviceAccount": owner_device}, json={}
        )
        user_id = sign_in.json()["user"]["id"]

        def _expired(with_binding: bool) -> str:
            data: dict[str, object] = {
                "store": "GUEST",
                "sessionId": str(uuid.uuid4()),
                "userId": user_id,
            }
            if with_binding:
                data["deviceHash"] = owner_device
            return jwt.encode(
                {
                    "kind": "SIGN_IN_SESSION_TOKEN",
                    "exp": int(time.time()) - 86400,
                    "data": data,
                },
                settings.require_jwt_secret(),
                algorithm="HS256",
            )

        bound = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": f"stranger-{uuid.uuid4()}"},
            json={"signInSessionToken": _expired(with_binding=True)},
        )
        unbound = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": f"stranger2-{uuid.uuid4()}"},
            json={"signInSessionToken": _expired(with_binding=False)},
        )

    # A **bound** token from a device it was not issued to is still refused: that is the
    # clearest sign of a copied token, and the caller keeps the explicit error.
    assert bound.status_code == 401
    assert bound.json()["code"] == "LCX_INVALID_SESSION_DEVICE"
    # An unbound one cannot prove anything, so the device is signed in as a new player.
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["userId"] != user_id

    async with _client() as client:
        owner = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": owner_device},
            json={"signInSessionToken": sign_in.json()["signInSessionToken"]},
        )
    assert owner.status_code == 200
    assert owner.json()["userId"] == user_id


@pytest.mark.asyncio
async def test_configured_shared_device_identifier_gives_each_install_its_own_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared identifier must not hand one account to every install.

    An Android build that cannot obtain a real device identity reports the same value
    to every install, so the second player lands in the first player's account and
    never sees the registration flow. Operators list such values in
    ``DANKAGU_SHARED_DEVICE_IDS``; the identifier is then ignored and each install
    registers its own account.
    """
    shared = f"shared-device-{uuid.uuid4()}"
    monkeypatch.setattr(settings, "shared_device_ids", shared, raising=False)

    async with _client() as client:
        first = await client.post("/identity/sign-in", headers={"deviceAccount": shared})
        second = await client.post("/identity/sign-in", headers={"deviceAccount": shared})

        first_user = first.json()["user"]["id"]
        second_user = second.json()["user"]["id"]

        # The identifier was not stored, so the installs are genuinely independent.
        first_player = await _load_player(first_user)
        second_player = await _load_player(second_user)

    assert first.status_code == 200 and second.status_code == 200
    assert first_user != second_user
    assert first_player is not None and first_player.device_account is None
    assert second_player is not None and second_player.device_account is None


@pytest.mark.asyncio
async def test_a_real_device_identifier_still_reuses_its_account() -> None:
    """iOS (and any client with a genuine id) keeps device-based recovery."""
    device = f"real-device-{uuid.uuid4()}"

    async with _client() as client:
        first = await client.post("/identity/sign-in", headers={"deviceAccount": device})
        second = await client.post("/identity/sign-in", headers={"deviceAccount": device})

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["user"]["id"] == second.json()["user"]["id"]


def test_sign_in_session_token_lasts_thirty_days() -> None:
    """The client-visible lifetime, which bounds how long a leaked token is useful."""
    assert settings.sign_in_session_ttl_seconds == 86400 * 30

    token = generate_lcx_tokens("ttl-user", 0, device_hash="ttl-device")["signInSessionToken"]
    payload = jwt.decode(token, settings.require_jwt_secret(), algorithms=["HS256"])
    remaining = payload["exp"] - int(time.time())
    assert 86400 * 29 < remaining <= 86400 * 30


@pytest.mark.asyncio
async def test_gcp_force_sign_in_path_is_served() -> None:
    """The SDK calls force-sign-in under /gcp/; that path must not 404."""
    async with _client() as client:
        res = await client.post(
            "/gcp/identity/force-sign-in",
            headers={"deviceAccount": f"gcp-force-{uuid.uuid4()}"},
            json={},
        )
    assert res.status_code == 200
    for field in ("accessToken", "signInSessionToken", "user"):
        assert res.json().get(field), f"missing {field}"


@pytest.mark.asyncio
async def test_token_without_a_binding_still_works_when_no_device_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to compare against means the token cannot be judged; keep it working."""
    async with _client() as client:
        sign_in = await client.post("/identity/sign-in", json={})
        assert sign_in.status_code == 200
        token = sign_in.json()["signInSessionToken"]

        refreshed = await client.post("/auth/user-access-token", json={"signInSessionToken": token})
    assert refreshed.status_code == 200
    assert refreshed.json()["userId"] == sign_in.json()["user"]["id"]


@pytest.mark.asyncio
async def test_rotating_the_signing_secret_invalidates_every_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only lever that revokes a stateless LCX token: rotate the secret."""
    async with _client() as client:
        sign_in = await client.post(
            "/identity/sign-in", headers={"deviceAccount": f"rotate-{uuid.uuid4()}"}
        )
        token = sign_in.json()["signInSessionToken"]
        victim_user = sign_in.json()["user"]["id"]

    monkeypatch.setattr(
        settings, "jwt_secret", "rotated-secret-that-nobody-holds-yet", raising=False
    )

    async with _client() as client:
        res = await client.post(
            "/auth/user-access-token",
            headers={"deviceAccount": f"rotate-{uuid.uuid4()}"},
            json={"signInSessionToken": token},
        )
    # The old token no longer proves an identity, so it cannot reach that account.
    assert res.status_code == 200
    assert res.json()["userId"] != victim_user


@pytest.mark.asyncio
async def test_shared_device_identifier_is_ignored() -> None:
    """The old global `storeAccountIdHash` constant must not identify a device."""
    shared = "dankagu-local-account-hash"

    async with _client() as client:
        first = await client.post(
            "/identity/sign-in", json={"storeAccountIdHash": shared, "deviceAccount": shared}
        )
        second = await client.post(
            "/identity/sign-in", json={"storeAccountIdHash": shared, "deviceAccount": shared}
        )

    assert first.status_code == 200 and second.status_code == 200
    first_player = await _load_player(first.json()["user"]["id"])
    second_player = await _load_player(second.json()["user"]["id"])
    assert first_player is not None and second_player is not None
    # The shared value must not have been stored as a device identity at all.
    assert first_player.device_account is None
    assert second_player.device_account is None
    assert first_player.user_id != second_player.user_id


def test_store_account_hash_is_per_player() -> None:
    """The value we hand to clients is derived per player, not a constant."""
    from dankagu.lcx.tokens import derive_store_account_hash, generate_lcx_tokens

    one = derive_store_account_hash("user-one")
    two = derive_store_account_hash("user-two")
    assert one != two

    tokens = generate_lcx_tokens("user-one", int(time.time()))
    payload = jwt.decode(
        tokens["signInSessionToken"], settings.require_jwt_secret(), algorithms=["HS256"]
    )
    assert payload["data"]["storeAccountIdHash"] == one
    assert payload["data"]["storeAccountIdHash"] != "dankagu-local-account-hash"


@pytest.mark.asyncio
async def test_android_fresh_installs_with_identical_verification_token_never_collide() -> None:
    """Two Android fresh installs without tokens reporting identical verificationToken and
    device_account must never enter the first player's onboarded account, and each must
    solidify on its own device across restarts.
    """
    import base64

    from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import system_pb2
    from dankagu.services.system import SystemService

    class _DummyContext:
        def invocation_metadata(self):
            return []

    shared_iss = base64.b64encode(f"com.dena.a12026801:{uuid.uuid4().hex}".encode()).decode("utf-8")
    vt = jwt.encode(
        {"iss": shared_iss, "aud": _BASE},
        "dummy-platform-secret-at-least-32-bytes",
        algorithm="HS256",
    )
    shared_takasho_device = f"TAKASHO-SHARED-{uuid.uuid4().hex[:16].upper()}"
    system_service = SystemService()
    ctx = _DummyContext()

    async with _client() as client:
        # 1. Android Device 1 fresh install: LCX sign-in + AuthorizeV3 + Onboarding
        sign_in_1 = await client.post("/identity/sign-in", json={"verificationToken": vt})
        assert sign_in_1.status_code == 200
        data_1 = sign_in_1.json()
        auth_1 = await system_service.AuthorizeV3(
            system_pb2.SystemAuthorizeV3.Request(
                id_token=data_1["lcxUserIdToken"],
                device_account=shared_takasho_device,
            ),
            ctx,  # type: ignore[arg-type]
        )
        player_1_id = auth_1.player_id

        async with get_sessionmaker()() as session:
            p1 = await session.get(Player, player_1_id)
            assert p1 is not None
            p1.nickname = "AndroidPlayerOne"
            p1.consented_tos_version = "v2.1.0"
            await session.commit()

        # 2. Android Device 2 fresh install (no token, identical verificationToken & device_account)
        sign_in_2 = await client.post("/identity/sign-in", json={"verificationToken": vt})
        assert sign_in_2.status_code == 200
        data_2 = sign_in_2.json()
        assert data_2["user"]["id"] != data_1["user"]["id"]

        auth_2 = await system_service.AuthorizeV3(
            system_pb2.SystemAuthorizeV3.Request(
                id_token=data_2["lcxUserIdToken"],
                device_account=shared_takasho_device,
            ),
            ctx,  # type: ignore[arg-type]
        )
        player_2_id = auth_2.player_id
        assert player_2_id != player_1_id

        # Mark Device 2 onboarded as well
        async with get_sessionmaker()() as session:
            p2 = await session.get(Player, player_2_id)
            assert p2 is not None
            assert p2.nickname == ""
            p2.nickname = "AndroidPlayerTwo"
            p2.consented_tos_version = "v2.1.0"
            await session.commit()

        # 3. Both devices restart multiple times -> each stays on its own account!
        for _ in range(2):
            refresh_1 = await client.post(
                "/auth/user-access-token",
                json={"signInSessionToken": data_1["signInSessionToken"], "verificationToken": vt},
            )
            id_tok_1 = (
                await client.post(f"/identity/user-id-token/{refresh_1.json()['userId']}")
            ).json()["lcxUserIdToken"]
            re_auth_1 = await system_service.AuthorizeV3(
                system_pb2.SystemAuthorizeV3.Request(
                    id_token=id_tok_1, device_account=shared_takasho_device
                ),
                ctx,  # type: ignore[arg-type]
            )
            assert re_auth_1.player_id == player_1_id

            refresh_2 = await client.post(
                "/auth/user-access-token",
                json={"signInSessionToken": data_2["signInSessionToken"], "verificationToken": vt},
            )
            id_tok_2 = (
                await client.post(f"/identity/user-id-token/{refresh_2.json()['userId']}")
            ).json()["lcxUserIdToken"]
            re_auth_2 = await system_service.AuthorizeV3(
                system_pb2.SystemAuthorizeV3.Request(
                    id_token=id_tok_2, device_account=shared_takasho_device
                ),
                ctx,  # type: ignore[arg-type]
            )
            assert re_auth_2.player_id == player_2_id


@pytest.mark.asyncio
async def test_authorize_v3_device_conflict_solidifies_replacement_account_across_restarts() -> (
    None
):
    """When Device B presents a copied signInSessionToken belonging to Device A's bound
    account, AuthorizeV3 separates Device B into a new account AND solidifies that account
    on Device B across all subsequent restarts (even after onboarding).
    """
    from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import system_pb2
    from dankagu.services.system import SystemService

    class _DummyContext:
        def invocation_metadata(self):
            return []

    system_service = SystemService()
    ctx = _DummyContext()
    takasho_a = f"TAKASHO-A-{uuid.uuid4().hex[:20].upper()}"
    takasho_b = f"TAKASHO-B-{uuid.uuid4().hex[:20].upper()}"

    async with _client() as client:
        # 1. Device A creates and binds its account in AuthorizeV3
        sign_in_a = await client.post("/identity/sign-in", json={})
        data_a = sign_in_a.json()
        auth_a = await system_service.AuthorizeV3(
            system_pb2.SystemAuthorizeV3.Request(
                id_token=data_a["lcxUserIdToken"],
                device_account=takasho_a,
            ),
            ctx,  # type: ignore[arg-type]
        )
        player_a_id = auth_a.player_id

        # 2. Device B boots with a copied signInSessionToken from Device A, but reports takasho_b
        refresh_b1 = await client.post(
            "/auth/user-access-token",
            json={"signInSessionToken": data_a["signInSessionToken"]},
        )
        id_tok_b1 = (
            await client.post(f"/identity/user-id-token/{refresh_b1.json()['userId']}")
        ).json()["lcxUserIdToken"]
        auth_b1 = await system_service.AuthorizeV3(
            system_pb2.SystemAuthorizeV3.Request(
                id_token=id_tok_b1,
                device_account=takasho_b,
            ),
            ctx,  # type: ignore[arg-type]
        )
        player_b_id = auth_b1.player_id
        assert player_b_id != player_a_id

        # Complete onboarding on Device B's new account
        async with get_sessionmaker()() as session:
            pb = await session.get(Player, player_b_id)
            assert pb is not None
            assert pb.device_account == takasho_b
            pb.nickname = "SolidifiedOnDeviceB"
            pb.consented_tos_version = "v2.1.0"
            await session.commit()

        # 3. Device B restarts the game multiple times -> always lands on player_b_id!
        for _ in range(3):
            refresh_b_next = await client.post(
                "/auth/user-access-token",
                json={"signInSessionToken": data_a["signInSessionToken"]},
            )
            id_tok_b_next = (
                await client.post(f"/identity/user-id-token/{refresh_b_next.json()['userId']}")
            ).json()["lcxUserIdToken"]
            auth_b_next = await system_service.AuthorizeV3(
                system_pb2.SystemAuthorizeV3.Request(
                    id_token=id_tok_b_next,
                    device_account=takasho_b,
                ),
                ctx,  # type: ignore[arg-type]
            )
            assert auth_b_next.player_id == player_b_id
