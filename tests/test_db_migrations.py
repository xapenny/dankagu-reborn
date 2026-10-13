"""Versioned database migrations: fresh installs, legacy adoption, rollback.

These tests exercise the paths that decide whether a live server survives an
upgrade, using a scratch database they create and drop themselves
(``dankagu_migrations_test``). They never touch ``dankagu`` (production) or
``dankagu_test`` (the suite database).
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text

from dankagu.config import settings
from dankagu.core.database import create_all_tables, get_engine, reset_engine
from dankagu.db import migrate
from dankagu.models.player import Player
from dankagu.models.storage import PlayerStorageEntry

SCRATCH_DB = "dankagu_migrations_test"
LEGACY_PLAYER_ID = "legacy-player-id"
SHORT_IDS = {"alice": "11111111", "bob": "22222222", "carol": "33333333"}


def _plain_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _async_url(base: str, database: str) -> str:
    return f"{base.replace('postgresql://', 'postgresql+asyncpg://')}/{database}"


def _base_url() -> str:
    """Server URL without the database name, derived from the suite's DB URL."""
    return _plain_url(settings.db_url).rsplit("/", 1)[0]


async def _drop_and_create_scratch() -> None:
    admin = await asyncpg.connect(f"{_base_url()}/postgres", timeout=5.0)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        await admin.close()


async def _drop_scratch() -> None:
    admin = await asyncpg.connect(f"{_base_url()}/postgres", timeout=5.0)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest_asyncio.fixture
async def scratch_db() -> AsyncGenerator[str, None]:
    """Point the application at an empty throwaway database."""
    original_url = settings.db_url
    await _drop_and_create_scratch()
    reset_engine()
    settings.db_url = _async_url(_base_url(), SCRATCH_DB)
    try:
        yield settings.db_url
    finally:
        reset_engine()
        settings.db_url = original_url
        reset_engine()
        await _drop_scratch()


async def _assert_no_schema_drift() -> None:
    """Fail when the database and the ORM models disagree.

    Uses the same object filter as ``env.py`` (and therefore as CI's
    ``alembic check``), but on the caller's event loop: Alembic's standalone path
    starts a second loop, which is flaky inside worker threads on Windows.
    """
    assert await migrate.schema_drift() == []


async def _relations() -> set[str]:
    engine = get_engine()
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {r[0] for r in rows}


async def _applied_revision() -> str | None:
    return await migrate.current_revision()


async def _scalar(sql: str, **params: object) -> object:
    engine = get_engine()
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def _fetch(sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = get_engine()
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(text(sql), params)).all()]


async def _insert_legacy_player() -> None:
    """Insert a player + storage row the way the pre-Alembic code did."""
    engine = get_engine()
    now = int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO players (id, user_id, device_account, short_id, name, nickname,"
                " birth_year, birth_month, consented_tos_version, level, exp, total_login_days,"
                " last_login_at, registered_at, is_banned, storage_revision)"
                " VALUES (:id, 'legacy-user', 'legacy-device', '87654321', 'Legacy', 'Legacy',"
                " 0, 0, '', 7, 0, 3, :now, :now, false, '1')"
            ),
            {"id": LEGACY_PLAYER_ID, "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO player_storage_entries (player_id, key, value, created_at, updated_at)"
                " VALUES (:id, 'PlayerBase--1', :value, :now, :now)"
            ),
            {
                "id": LEGACY_PLAYER_ID,
                # Synthetic {1: 1, 2: 1, 3: "world"}; only non-emptiness matters.
                "value": bytes.fromhex("080110011a05776f726c64"),
                "now": now,
            },
        )


async def test_fresh_database_upgrades_to_head(scratch_db: str) -> None:
    """An empty database gets the full schema and is stamped at the baseline."""
    result = await migrate.run_migrations()

    assert result.applied is True
    assert result.stamped_baseline is False
    assert result.from_revision is None
    assert result.to_revision == migrate.head_revision()
    assert await _applied_revision() == migrate.head_revision()

    tables = await _relations()
    assert {"players", "player_storage_entries", "friend_relations", "sessions"} <= tables

    # The migrations must describe the ORM models exactly, otherwise every later
    # `alembic check` (and therefore CI) would report drift.
    await _assert_no_schema_drift()


async def test_rerun_is_a_noop(scratch_db: str) -> None:
    """A second start must not re-apply anything (deployments restart often)."""
    await migrate.run_migrations()
    second = await migrate.run_migrations()

    assert second.applied is False
    assert second.to_revision == migrate.head_revision()
    assert second.stamped_baseline is False


async def test_legacy_database_is_stamped_and_keeps_player_data(scratch_db: str) -> None:
    """A pre-Alembic database is adopted, never rebuilt."""
    # Exactly what the old startup path did: create_all + data, no version table.
    await create_all_tables()
    await _insert_legacy_player()

    result = await migrate.run_migrations()

    assert result.stamped_baseline is True
    assert result.from_revision is None
    assert result.to_revision == migrate.head_revision()
    assert await _applied_revision() == migrate.head_revision()

    nickname = await _scalar("SELECT nickname FROM players")
    storage_rows = await _scalar("SELECT count(*) FROM player_storage_entries")
    assert nickname == "Legacy"
    assert storage_rows == 1
    # Stamping must not leave the schema half-described.
    await _assert_no_schema_drift()


async def test_downgrade_and_upgrade_round_trip(scratch_db: str) -> None:
    """`downgrade base` must really remove the schema, and upgrade rebuild it."""
    await migrate.run_migrations()
    assert "players" in await _relations()

    downgraded = await migrate.downgrade("base")
    assert downgraded.to_revision is None
    assert "players" not in await _relations()

    upgraded = await migrate.run_migrations()
    assert upgraded.to_revision == migrate.head_revision()
    assert "players" in await _relations()


async def test_second_instance_waits_for_the_migration_lock(scratch_db: str) -> None:
    """Concurrent starts serialise; a holder that never releases fails startup."""
    holder = await asyncpg.connect(_plain_url(scratch_db), timeout=5.0)
    try:
        await holder.execute("SELECT pg_advisory_lock($1)", migrate.ADVISORY_LOCK_KEY)
        with pytest.raises(migrate.MigrationLockTimeout):
            await migrate.run_migrations(wait_seconds=0)
    finally:
        await holder.execute("SELECT pg_advisory_unlock($1)", migrate.ADVISORY_LOCK_KEY)
        await holder.close()

    # Once the holder is gone the migration completes normally.
    assert (await migrate.run_migrations(wait_seconds=0)).to_revision == migrate.head_revision()


async def test_player_model_is_usable_after_migration(scratch_db: str) -> None:
    """Smoke test: the migrated schema accepts the ORM's own inserts."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    await migrate.run_migrations()
    now = int(time.time())
    engine = get_engine()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        player = Player(
            user_id="migrated-user",
            device_account="migrated-device",
            short_id="11223344",
            name="Migrated",
            nickname="Migrated",
            registered_at=now,
            last_login_at=now,
        )
        session.add(player)
        await session.flush()
        session.add(
            PlayerStorageEntry(
                player_id=player.id,
                key="PlayerBase--1",
                value=b"\x08\x01",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
        assert player.id


class TestFriendRelationsRepair:
    """``0002_friend_relations_normalize`` against rows a live server could hold."""

    @staticmethod
    async def _seed() -> dict[str, str]:
        """Upgrade to the baseline only, then plant every kind of bad row."""
        await migrate.run_migrations(migrate.BASELINE_REVISION)
        assert await _applied_revision() == migrate.BASELINE_REVISION

        ids = {
            "alice": "alice-id-0000-0000-0000-000000000001",
            "bob": "bob-id-0000-0000-0000-000000000002",
            "carol": "carol-id-0000-0000-0000-000000000003",
        }
        engine = get_engine()
        now = int(time.time())
        async with engine.begin() as conn:
            for name, player_id in ids.items():
                await conn.execute(
                    text(
                        "INSERT INTO players (id, user_id, device_account, short_id, name, nickname,"
                        " birth_year, birth_month, consented_tos_version, level, exp, total_login_days,"
                        " last_login_at, registered_at, is_banned, storage_revision)"
                        " VALUES (:id, :user_id, :device, :short_id, :name, :name, 0, 0, '', 1, 0, 1,"
                        " :now, :now, false, '1')"
                    ),
                    {
                        "id": player_id,
                        "user_id": f"user-{name}",
                        "device": f"device-{name}",
                        "short_id": SHORT_IDS[name],
                        "name": name,
                        "now": now,
                    },
                )
            relations = [
                # 1. already correct
                (ids["alice"], ids["bob"], 1),
                # 2. stored under the 8-digit プレイヤーID (the reported bug);
                #    normalising it turns this into a duplicate of row 1, because
                #    uq_friend_follower_target predates Alembic and already
                #    rejects an identical pair.
                (SHORT_IDS["alice"], ids["bob"], 2),
                # 3. follower stored under the 8-digit id (no collision)
                (SHORT_IDS["carol"], ids["alice"], 3),
                # 4. target stored under the LCX user id (no collision)
                (ids["bob"], "user-carol", 4),
                # 5. orphan: the follower account was deleted
                ("deleted-player-id", ids["bob"], 5),
            ]
            for follower, target, created_at in relations:
                await conn.execute(
                    text(
                        "INSERT INTO friend_relations (follower_id, target_id, created_at)"
                        " VALUES (:follower, :target, :created_at)"
                    ),
                    {"follower": follower, "target": target, "created_at": created_at},
                )
        return ids

    async def test_upgrade_normalises_and_constrains(self, scratch_db: str) -> None:
        ids = await self._seed()

        result = await migrate.run_migrations()

        assert result.to_revision == migrate.head_revision()
        rows = await _fetch(
            "SELECT follower_id, target_id FROM friend_relations ORDER BY created_at"
        )
        # The duplicate (row 2), the rewritten pair and the orphan's removal leave
        # these three; every reference is a players.id.
        assert rows == [
            (ids["alice"], ids["bob"]),
            (ids["carol"], ids["alice"]),
            (ids["bob"], ids["carol"]),
        ]

        assert await _fetch("SELECT follower_id, target_id FROM friend_relations_orphans") == [
            ("deleted-player-id", ids["bob"])
        ]

        logged = dict(
            await _fetch("SELECT action, count(*) FROM friend_relations_repair_log GROUP BY action")
        )
        assert logged["rewrite"] == 2  # short_id + user_id
        assert logged["duplicate"] == 1
        assert logged["orphan"] == 1

        # Both foreign keys exist and are validated, so an orphan cannot return.
        assert await _fetch(
            "SELECT conname, convalidated FROM pg_constraint"
            " WHERE conrelid = 'friend_relations'::regclass AND contype = 'f' ORDER BY conname"
        ) == [("fk_friend_follower", True), ("fk_friend_target", True)]

        await _assert_no_schema_drift()

    async def test_downgrade_restores_orphans_and_references(self, scratch_db: str) -> None:
        ids = await self._seed()
        await migrate.run_migrations()

        await migrate.downgrade(migrate.BASELINE_REVISION)

        # Orphans are back and the original reference strings are restored. The
        # deleted duplicate stays deleted by design (see the revision docstring);
        # it lives in friend_relations_backup_0002.
        assert await _scalar("SELECT count(*) FROM friend_relations") == 4
        assert (
            await _scalar(
                "SELECT count(*) FROM friend_relations"
                " WHERE follower_id = :short_carol OR target_id = 'user-carol'",
                short_carol=SHORT_IDS["carol"],
            )
            == 2
        )

        # Re-applying the revision produces the same clean state.
        await migrate.run_migrations()
        assert await _fetch(
            "SELECT follower_id, target_id FROM friend_relations ORDER BY created_at"
        ) == [
            (ids["alice"], ids["bob"]),
            (ids["carol"], ids["alice"]),
            (ids["bob"], ids["carol"]),
        ]
        await _assert_no_schema_drift()

    async def test_deleting_a_player_cascades_its_relations(self, scratch_db: str) -> None:
        """The new foreign keys remove relations when a player is deleted."""
        ids = await self._seed()
        await migrate.run_migrations()

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM players WHERE id = :id"), {"id": ids["bob"]})

        # Only the relation between the two surviving players is left.
        assert await _fetch("SELECT follower_id, target_id FROM friend_relations") == [
            (ids["carol"], ids["alice"])
        ]
