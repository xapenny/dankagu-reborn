"""Versioned database migrations (Alembic) with legacy-database bootstrap.

Deployment contract
-------------------
* :func:`run_migrations` runs once during startup, **before any listener binds**,
  when ``DANKAGU_DB_AUTO_MIGRATE`` is true (default), or explicitly through
  ``python -m dankagu.db.migrate``.
* A database that predates Alembic (application tables present, no
  ``alembic_version``) is **stamped** with :data:`BASELINE_REVISION` instead of
  being rebuilt, so live player data is never touched and no DDL is re-applied.
* The whole run happens on one connection holding a PostgreSQL advisory lock, so
  concurrent instances / restarts serialise instead of racing on DDL.
* Failures propagate: the process must not start against a half-migrated schema.

Authoring rules for individual revisions live in ``src/dankagu/db/README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

from dankagu.config import settings
from dankagu.core.database import get_engine

logger = logging.getLogger("dankagu.db.migrate")

#: Revision describing the schema as it existed before Alembic was introduced.
#: Any database created by ``Base.metadata.create_all`` is stamped with it.
BASELINE_REVISION = "0001_baseline"

#: Fixed advisory-lock key ("DKMG"): every instance of this application uses it.
ADVISORY_LOCK_KEY = 0x444B4D47

#: Table that proves an application schema already exists.
_LEGACY_SENTINEL_TABLE = "players"

#: Version table written by Alembic.
_VERSION_TABLE = "alembic_version"

#: Per-DDL lock wait budget. Long backfills are expected; migrations that block
#: on a lock for longer than this should be rewritten, not waited out.
_DDL_LOCK_TIMEOUT_MS = 15_000

_LOCK_POLL_SECONDS = 1.0


class MigrationLockTimeout(RuntimeError):
    """Raised when another instance held the migration lock for too long."""


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of one migration run, for logging and tests."""

    from_revision: str | None
    to_revision: str | None
    target_revision: str | None
    stamped_baseline: bool
    applied: bool
    duration_ms: int

    def describe(self) -> str:
        parts = [f"{self.from_revision or 'base'} -> {self.to_revision or 'base'}"]
        if self.stamped_baseline:
            parts.append("legacy schema stamped with baseline")
        parts.append("applied" if self.applied else "already up to date")
        parts.append(f"{self.duration_ms} ms")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def migrations_dir() -> Path:
    """Filesystem location of the revision scripts (inside the package)."""
    return Path(__file__).resolve().parent / "migrations"


def alembic_config() -> Config:
    """Build an Alembic config bound to the application's database URL.

    Works both from a source checkout (``alembic.ini`` at the project root) and
    from an installed wheel, where the scripts are resolved from the package.
    """
    ini_path = settings.base_dir / "alembic.ini"
    cfg = Config(str(ini_path) if ini_path.exists() else None)
    # The caller owns logging: without this, env.py's fileConfig() would reset the
    # root logger to alembic.ini's level and swallow the migration summary.
    cfg.attributes["configure_logger"] = False
    # Explicit script_location keeps the CLI working from any working directory
    # and guarantees the packaged revisions win over a stray directory.
    cfg.set_main_option("script_location", str(migrations_dir()))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    return cfg


def head_revision(cfg: Config | None = None) -> str | None:
    """Resolve the single head revision of the migration script directory."""
    return ScriptDirectory.from_config(cfg or alembic_config()).get_current_head()


# ---------------------------------------------------------------------------
# Sync building blocks (run inside ``Connection.run_sync``)
# ---------------------------------------------------------------------------


def _read_revision(sync_conn: Connection) -> str | None:
    """Current ``alembic_version`` value, or ``None`` when the table is absent."""
    return MigrationContext.configure(sync_conn).get_current_revision()


def _relation_exists(sync_conn: Connection, table: str) -> bool:
    return (
        sync_conn.execute(text("SELECT to_regclass(:n)"), {"n": f"public.{table}"}).scalar()
        is not None
    )


def _is_legacy_database(sync_conn: Connection) -> bool:
    """True for a pre-Alembic database: schema present, no version table."""
    return _relation_exists(sync_conn, _LEGACY_SENTINEL_TABLE) and not _relation_exists(
        sync_conn, _VERSION_TABLE
    )


def _acquire_migration_lock(sync_conn: Connection, wait_seconds: int) -> None:
    """Take the session-level advisory lock, polling instead of blocking forever.

    Called from ``run_sync`` during startup, where blocking the (otherwise idle)
    event loop for a few seconds is preferable to a lock-timeout error.
    """
    deadline = time.monotonic() + max(wait_seconds, 0)
    warned = False
    while True:
        acquired = sync_conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
        ).scalar()
        # Leave no implicit transaction behind: Alembic must own its transactions.
        sync_conn.commit()
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeout(
                "another instance is still migrating "
                f"(waited {wait_seconds}s for advisory lock {ADVISORY_LOCK_KEY:#x}); "
                "increase DANKAGU_DB_MIGRATE_WAIT_SECONDS if this is expected"
            )
        if not warned:
            logger.info(
                "🤝 Another instance holds the migration lock; waiting up to %ds...",
                wait_seconds,
            )
            warned = True
        time.sleep(_LOCK_POLL_SECONDS)


def _release_migration_lock(sync_conn: Connection) -> None:
    try:
        sync_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
    finally:
        sync_conn.commit()


def _invoke(cfg: Config, sync_conn: Connection, action: Callable[[], None]) -> None:
    """Run an Alembic command on the caller's connection (shared-connection recipe)."""
    cfg.attributes["connection"] = sync_conn
    try:
        action()
    finally:
        cfg.attributes.pop("connection", None)
        if sync_conn.in_transaction():
            sync_conn.commit()


def _run_locked[T](sync_conn: Connection, wait_seconds: int, work: Callable[[], T]) -> T:
    """Serialize ``work`` behind the migration lock with a bounded DDL lock wait."""
    _acquire_migration_lock(sync_conn, wait_seconds)
    try:
        sync_conn.exec_driver_sql(f"SET lock_timeout = '{_DDL_LOCK_TIMEOUT_MS}ms'")
        sync_conn.commit()  # session-level SET survives the commit
        return work()
    finally:
        _release_migration_lock(sync_conn)


def _upgrade_sync(
    sync_conn: Connection,
    cfg: Config,
    revision: str,
    *,
    stamp_baseline: bool,
) -> MigrationResult:
    started = time.perf_counter()
    from_revision = _read_revision(sync_conn)
    stamped = False

    if stamp_baseline and _is_legacy_database(sync_conn):
        logger.warning(
            "🏛️  Legacy database detected (application tables without %s): "
            "stamping it with %s instead of re-creating the schema. "
            "No player data is modified.",
            _VERSION_TABLE,
            BASELINE_REVISION,
        )
        _invoke(cfg, sync_conn, lambda: command.stamp(cfg, BASELINE_REVISION))
        stamped = True

    revision_after_stamp = BASELINE_REVISION if stamped else from_revision
    _invoke(cfg, sync_conn, lambda: command.upgrade(cfg, revision))
    to_revision = _read_revision(sync_conn)

    return MigrationResult(
        from_revision=from_revision,
        to_revision=to_revision,
        target_revision=revision,
        stamped_baseline=stamped,
        applied=to_revision != revision_after_stamp,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _downgrade_sync(sync_conn: Connection, cfg: Config, revision: str) -> MigrationResult:
    started = time.perf_counter()
    from_revision = _read_revision(sync_conn)
    _invoke(cfg, sync_conn, lambda: command.downgrade(cfg, revision))
    to_revision = _read_revision(sync_conn)
    return MigrationResult(
        from_revision=from_revision,
        to_revision=to_revision,
        target_revision=revision,
        stamped_baseline=False,
        applied=to_revision != from_revision,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _stamp_sync(sync_conn: Connection, cfg: Config, revision: str) -> str | None:
    _invoke(cfg, sync_conn, lambda: command.stamp(cfg, revision))
    return _read_revision(sync_conn)


# ---------------------------------------------------------------------------
# Async API used by the application and the CLI
# ---------------------------------------------------------------------------


async def run_migrations(
    revision: str = "head",
    *,
    wait_seconds: int | None = None,
    stamp_baseline: bool = True,
) -> MigrationResult:
    """Bring the database to ``revision``, bootstrapping a legacy schema first."""
    return await _with_engine(
        lambda sync_conn, cfg: _run_locked(
            sync_conn,
            settings.db_migrate_wait_seconds if wait_seconds is None else wait_seconds,
            lambda: _upgrade_sync(sync_conn, cfg, revision, stamp_baseline=stamp_baseline),
        )
    )


async def downgrade(revision: str, *, wait_seconds: int | None = None) -> MigrationResult:
    """Roll the schema back to ``revision`` (used by operators and CI)."""
    return await _with_engine(
        lambda sync_conn, cfg: _run_locked(
            sync_conn,
            settings.db_migrate_wait_seconds if wait_seconds is None else wait_seconds,
            lambda: _downgrade_sync(sync_conn, cfg, revision),
        )
    )


async def stamp(revision: str, *, wait_seconds: int | None = None) -> str | None:
    """Record ``revision`` as applied without running it (bootstrap / recovery)."""
    return await _with_engine(
        lambda sync_conn, cfg: _run_locked(
            sync_conn,
            settings.db_migrate_wait_seconds if wait_seconds is None else wait_seconds,
            lambda: _stamp_sync(sync_conn, cfg, revision),
        )
    )


async def current_revision() -> str | None:
    """Revision recorded in the database, or ``None`` for an unmigrated one."""
    return await _with_engine(lambda sync_conn, _cfg: _read_revision(sync_conn))


async def _with_engine[T](action: Callable[[Connection, Config], T]) -> T:
    engine = get_engine()
    cfg = alembic_config()
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: action(sync_conn, cfg))


def check_models_match_migrations() -> None:
    """Raise ``AutogenerateDiffsDetected`` when the ORM drifted from the revisions.

    Runs Alembic's standalone path, which drives its own event loop. Call it from
    synchronous code (the CLI) or from CI - **not** from inside a running event
    loop, and prefer :func:`schema_drift` from async code: spinning up a second
    loop inside a worker thread is unreliable on Windows.
    """
    command.check(alembic_config())


async def schema_drift() -> list[object]:
    """Schema differences between the database and the ORM metadata.

    Empty when the database matches the models. Uses the caller's event loop and
    the same object filter as ``env.py``, so an async test can assert on drift
    without starting Alembic's standalone machinery.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from dankagu.core.database import Base
    from dankagu.db.schema_scope import include_object

    def _compare(sync_conn: Connection) -> list[object]:
        context = MigrationContext.configure(
            sync_conn,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": include_object,
            },
        )
        return list(compare_metadata(context, Base.metadata))

    return await _with_engine(lambda sync_conn, _cfg: _compare(sync_conn))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _configure_cli_logging() -> None:
    """Log to stdout as UTF-8 so status emoji survive a non-UTF-8 console code page.

    ``reconfigure`` is used instead of wrapping ``sys.stdout.buffer`` in a second
    ``TextIOWrapper``: a discarded wrapper closes the shared buffer, which breaks
    any later writer - including Alembic's own ``Config.stdout``.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - exotic stdout replacement
            pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dankagu.db.migrate",
        description=(
            "Apply versioned database migrations. Also runs automatically at "
            "startup when DANKAGU_DB_AUTO_MIGRATE=true."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--upgrade",
        nargs="?",
        const="head",
        metavar="REVISION",
        help="upgrade to REVISION (default: head); this is also the no-argument default",
    )
    group.add_argument("--downgrade", metavar="REVISION", help="downgrade to REVISION")
    group.add_argument(
        "--stamp", metavar="REVISION", help="mark REVISION applied without running it"
    )
    group.add_argument("--current", action="store_true", help="print the applied revision")
    group.add_argument("--head", action="store_true", help="print the latest available revision")
    group.add_argument("--history", action="store_true", help="print the revision history")
    group.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the ORM models differ from the migration history",
    )
    args = parser.parse_args(argv)

    _configure_cli_logging()

    cfg = alembic_config()

    if args.check:
        from alembic.util.exc import AutogenerateDiffsDetected

        try:
            check_models_match_migrations()
        except AutogenerateDiffsDetected as exc:
            logger.error("❌ ORM models differ from the migration history: %s", exc)
            return 1
        logger.info("✅ Migrations match the ORM models.")
        return 0

    if args.history:
        command.history(cfg, verbose=True)
        return 0

    if args.head:
        print(head_revision(cfg) or "")
        return 0

    if args.current:
        print(asyncio.run(current_revision()) or "")
        return 0

    if args.downgrade:
        result = asyncio.run(downgrade(args.downgrade))
        logger.info("↩️  Downgrade %s", result.describe())
        return 0

    if args.stamp:
        applied = asyncio.run(stamp(args.stamp))
        logger.info("🏷️  Stamped %s (now at %s)", args.stamp, applied)
        return 0

    revision = args.upgrade or "head"
    result = asyncio.run(run_migrations(revision))
    logger.info("🗄️  Migration %s", result.describe())
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess/CI
    raise SystemExit(main())
