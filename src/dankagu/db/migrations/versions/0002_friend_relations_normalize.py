"""friend_relations: normalise player references, then constrain them.

Older builds stored whatever the client sent in ``follower_id`` / ``target_id``.
Because the client holds the 8-digit プレイヤーID in the search flows and the UUID
in the follow flows, some rows point at a ``short_id`` instead of ``players.id``.
Such a row is invisible to both lists (the follower list matches on ``target_id ==
my players.id``, the following list joins on ``Player.id``) and the follow status
reports "not following" - silently, with the client believing the follow worked.

This revision:

1. snapshots the table, then logs every change it makes (audit tables are hidden
   from autogenerate, see ``dankagu.db.schema_scope``);
2. rewrites ``short_id`` / ``user_id`` references to ``players.id`` in batches;
3. removes duplicate pairs (keeping the lowest id);
4. quarantines rows whose reference no longer resolves to a player;
5. adds ``ON DELETE CASCADE`` foreign keys, ``NOT VALID`` first and then validated
   in a separate statement (safe here because step 4 proved every remaining row
   resolves; a million-row table should split the two statements into separate
   revisions).

``downgrade()`` restores the quarantine and the rewritten ids. Deleted duplicates
are **not** restored automatically - the ``friend_relations_backup_0002`` snapshot
holds them for manual recovery, because re-inserting them would violate the unique
constraint the application relies on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_friend_relations_normalize"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Rows rewritten per statement. Keeps every transaction short enough that the
#: DDL lock timeout is never reached on a large table.
_BATCH = 2000

_LOG_TABLE = "friend_relations_repair_log"
_ORPHAN_TABLE = "friend_relations_orphans"
_BACKUP_TABLE = "friend_relations_backup_0002"

_REFERENCES = {"follower_id", "target_id"}
_FOREIGN_KEYS = (("fk_friend_follower", "follower_id"), ("fk_friend_target", "target_id"))


def _connect() -> sa.Connection:
    return op.get_bind()


def _create_audit_tables() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE TABLE IF NOT EXISTS {_LOG_TABLE} (
                id BIGSERIAL PRIMARY KEY,
                rel_id INTEGER,
                column_name VARCHAR(32) NOT NULL,
                old_value VARCHAR(64),
                new_value VARCHAR(64),
                action VARCHAR(16) NOT NULL,
                resolved_by VARCHAR(16),
                at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    # Quarantine + snapshot. Declared explicitly rather than with
    # `LIKE friend_relations INCLUDING ALL`: copying the id default would make
    # these tables depend on friend_relations_id_seq, and dropping the base table
    # (downgrade to base) would then fail. They stay readable after a rollback.
    for table in (_ORPHAN_TABLE, _BACKUP_TABLE):
        op.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id INTEGER,
                    follower_id VARCHAR(64) NOT NULL,
                    target_id VARCHAR(64) NOT NULL,
                    created_at BIGINT NOT NULL
                )
                """
            )
        )
    op.execute(
        sa.text(
            f"INSERT INTO {_BACKUP_TABLE} (id, follower_id, target_id, created_at) "
            f"SELECT id, follower_id, target_id, created_at FROM friend_relations "
            f"WHERE NOT EXISTS (SELECT 1 FROM {_BACKUP_TABLE})"
        )
    )


def _rewrite_reference(column: str, match_column: str) -> int:
    """Point ``column`` at ``players.id`` wherever it holds another id form."""
    if column not in _REFERENCES or match_column not in {"short_id", "user_id"}:
        raise ValueError(f"unsupported rewrite: {column} <- {match_column}")

    rewritten = 0
    statement = sa.text(
        f"""
        WITH target AS (
            SELECT r.id AS rel_id, p.id AS new_value, r.{column} AS old_value
              FROM friend_relations r
              JOIN players p ON r.{column} = p.{match_column}
             WHERE r.{column} <> p.id
             LIMIT {_BATCH}
        ), updated AS (
            UPDATE friend_relations r
               SET {column} = t.new_value
              FROM target t
             WHERE r.id = t.rel_id
            RETURNING r.id AS rel_id
        ), logged AS (
            INSERT INTO {_LOG_TABLE}
                (rel_id, column_name, old_value, new_value, action, resolved_by)
            SELECT t.rel_id, '{column}', t.old_value, t.new_value, 'rewrite', '{match_column}'
              FROM target t
              JOIN updated u ON u.rel_id = t.rel_id
            RETURNING 1
        )
        SELECT count(*) FROM updated
        """
    )
    while True:
        moved = int(_connect().execute(statement).scalar() or 0)
        rewritten += moved
        if moved == 0:
            return rewritten


def _delete_normalised_duplicates() -> int:
    """Remove rows that would collide with an older row once normalised.

    ``_rewrite_reference`` sets a column to ``players.id``; when a raw-reference
    row normalises onto a pair that already exists, that UPDATE would violate
    ``uq_friend_follower_target`` (the constraint predates Alembic). Keeping the
    lowest id - the oldest relation - is the same rule as ``_delete_duplicates``.
    """
    removed_total = 0
    statement = sa.text(
        f"""
        WITH norm AS (
            SELECT DISTINCT ON (r.id)
                   r.id AS rel_id,
                   r.follower_id AS raw_follower,
                   r.target_id AS raw_target,
                   coalesce(pf.id, r.follower_id) AS follower_pk,
                   coalesce(pt.id, r.target_id) AS target_pk
              FROM friend_relations r
              LEFT JOIN players pf
                     ON pf.id = r.follower_id
                     OR pf.short_id = r.follower_id
                     OR pf.user_id = r.follower_id
              LEFT JOIN players pt
                     ON pt.id = r.target_id
                     OR pt.short_id = r.target_id
                     OR pt.user_id = r.target_id
             ORDER BY r.id
        ), doomed AS (
            SELECT a.rel_id, a.raw_follower, a.raw_target
              FROM norm a
              JOIN norm b
                ON a.follower_pk = b.follower_pk
               AND a.target_pk = b.target_pk
               AND a.rel_id > b.rel_id
             LIMIT {_BATCH}
        ), removed AS (
            DELETE FROM friend_relations r
                  USING doomed d
                  WHERE r.id = d.rel_id
            RETURNING r.id
        ), logged AS (
            INSERT INTO {_LOG_TABLE} (rel_id, column_name, old_value, new_value, action)
            SELECT d.rel_id, 'row', d.raw_follower, d.raw_target, 'duplicate'
              FROM doomed d
              JOIN removed x ON x.id = d.rel_id
            RETURNING 1
        )
        SELECT count(*) FROM removed
        """
    )
    while True:
        removed = int(_connect().execute(statement).scalar() or 0)
        removed_total += removed
        if removed == 0:
            return removed_total


def _delete_duplicates() -> int:
    """Drop rows that duplicate an earlier (follower, target) pair."""
    removed_total = 0
    statement = sa.text(
        f"""
        WITH doomed AS (
            SELECT a.id AS rel_id, a.follower_id, a.target_id
              FROM friend_relations a
              JOIN friend_relations b
                ON a.follower_id = b.follower_id
               AND a.target_id = b.target_id
               AND a.id > b.id
             LIMIT {_BATCH}
        ), removed AS (
            DELETE FROM friend_relations r
                  USING doomed d
                  WHERE r.id = d.rel_id
            RETURNING r.id
        ), logged AS (
            INSERT INTO {_LOG_TABLE} (rel_id, column_name, old_value, new_value, action)
            SELECT d.rel_id, 'row', d.follower_id, d.target_id, 'duplicate'
              FROM doomed d
              JOIN removed x ON x.id = d.rel_id
            RETURNING 1
        )
        SELECT count(*) FROM removed
        """
    )
    while True:
        removed = int(_connect().execute(statement).scalar() or 0)
        removed_total += removed
        if removed == 0:
            return removed_total


def _quarantine_orphans() -> int:
    """Move relations that no longer resolve to a player out of the table."""
    moved_total = 0
    statement = sa.text(
        f"""
        WITH doomed AS (
            SELECT r.id, r.follower_id, r.target_id, r.created_at
              FROM friend_relations r
             WHERE NOT EXISTS (SELECT 1 FROM players p WHERE p.id = r.follower_id)
                OR NOT EXISTS (SELECT 1 FROM players p WHERE p.id = r.target_id)
             LIMIT {_BATCH}
        ), moved AS (
            INSERT INTO {_ORPHAN_TABLE} (id, follower_id, target_id, created_at)
            SELECT id, follower_id, target_id, created_at FROM doomed
            ON CONFLICT DO NOTHING
            RETURNING id, follower_id, target_id
        ), removed AS (
            DELETE FROM friend_relations r
                  USING moved m
                  WHERE r.id = m.id
            RETURNING r.id
        ), logged AS (
            INSERT INTO {_LOG_TABLE} (rel_id, column_name, old_value, new_value, action)
            SELECT m.id, 'row', m.follower_id, m.target_id, 'orphan'
              FROM moved m
            RETURNING 1
        )
        SELECT count(*) FROM removed
        """
    )
    while True:
        moved = int(_connect().execute(statement).scalar() or 0)
        moved_total += moved
        if moved == 0:
            return moved_total


def _add_foreign_keys(validate: bool = True) -> None:
    for name, column in _FOREIGN_KEYS:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
                        ALTER TABLE friend_relations
                            ADD CONSTRAINT {name} FOREIGN KEY ({column})
                            REFERENCES players(id) ON DELETE CASCADE NOT VALID;
                    END IF;
                END
                $$;
                """
            )
        )
        if validate:
            # Separate statement on purpose: the orphan pass above proved every
            # remaining row resolves, so this scan is immediate.
            op.execute(sa.text(f"ALTER TABLE friend_relations VALIDATE CONSTRAINT {name}"))


def upgrade() -> None:
    _create_audit_tables()

    # Order matters: normalising a raw reference can collide with an existing
    # pair, so duplicates must go first.
    duplicates = _delete_normalised_duplicates()

    rewritten = _rewrite_reference("follower_id", "short_id")
    rewritten += _rewrite_reference("target_id", "short_id")
    rewritten += _rewrite_reference("follower_id", "user_id")
    rewritten += _rewrite_reference("target_id", "user_id")

    # Safety net for rows that were already duplicates without a rewrite.
    duplicates += _delete_duplicates()
    orphans = _quarantine_orphans()
    _add_foreign_keys()

    op.execute(
        sa.text(
            f"INSERT INTO {_LOG_TABLE} (rel_id, column_name, old_value, new_value, action, resolved_by) "
            "VALUES (NULL, 'summary', :rewritten, :duplicates, 'summary', :orphans)"
        ).bindparams(rewritten=str(rewritten), duplicates=str(duplicates), orphans=str(orphans))
    )


def downgrade() -> None:
    for name, _column in _FOREIGN_KEYS:
        op.execute(sa.text(f"ALTER TABLE friend_relations DROP CONSTRAINT IF EXISTS {name}"))

    # Restore the quarantined rows, then the original reference strings. Orphans
    # go back first so that ids line up again before the rewrite is undone.
    op.execute(
        sa.text(
            f"INSERT INTO friend_relations (id, follower_id, target_id, created_at) "
            f"SELECT id, follower_id, target_id, created_at FROM {_ORPHAN_TABLE} "
            f"ON CONFLICT DO NOTHING"
        )
    )
    for column in sorted(_REFERENCES):
        op.execute(
            sa.text(
                f"""
                UPDATE friend_relations r
                   SET {column} = l.old_value
                  FROM (
                        SELECT DISTINCT ON (rel_id) rel_id, old_value
                          FROM {_LOG_TABLE}
                         WHERE column_name = '{column}' AND action = 'rewrite'
                         ORDER BY rel_id, id DESC
                       ) l
                 WHERE r.id = l.rel_id
                """
            )
        )
