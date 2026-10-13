"""Constrain the player-scoped tables that had no foreign key at all.

Deleting an account relied on an application-level list of tables, and four
player-scoped tables were missing from any constraint: ``player_storage_entries``,
``player_photos``, ``photo_likes`` and ``my_space_likes``. Whatever the code forgot
stayed in the database, so "delete my account" did not actually delete the save data,
and likes or photos could point at players that no longer exist.

This revision, for each of those references:

1. deletes the rows that do not resolve (they are unreachable leftovers: no player can
   read them, and they only inflate counters), reporting how many it removed;
2. adds the foreign key ``NOT VALID`` so the existing table is not scanned under an
   exclusive lock;
3. validates it in a separate statement.

Every key is ``ON DELETE CASCADE``: closing an account takes its state with it.

``downgrade()`` drops the constraints again. The deleted rows are not restored --
they referenced players that no longer existed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_player_scoped_fks"
down_revision: str | None = "0004_purchase_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (constraint name, table, column, referenced table, referenced column)
_CONSTRAINTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("fk_storage_player", "player_storage_entries", "player_id", "players", "id"),
    ("fk_photo_player", "player_photos", "player_id", "players", "id"),
    ("fk_photo_like_player", "photo_likes", "player_id", "players", "id"),
    (
        "fk_photo_like_photo",
        "photo_likes",
        "photo_id",
        "player_photos",
        "photo_id",
    ),
    ("fk_myspace_like_from", "my_space_likes", "from_player_id", "players", "id"),
    ("fk_myspace_like_to", "my_space_likes", "to_player_id", "players", "id"),
)


def _delete_orphans(table: str, column: str, ref_table: str, ref_column: str) -> None:
    if not _has_table(table) or not _has_table(ref_table):
        return
    connection = op.get_bind()
    result = connection.execute(
        sa.text(
            f"DELETE FROM {table} AS t "
            f"WHERE t.{column} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {ref_table} AS r WHERE r.{ref_column} = t.{column})"
        )
    )
    removed = result.rowcount or 0
    if removed:
        print(f"  {table}.{column}: removed {removed} orphan row(s)")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _constraint_exists(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return any(fk.get("name") == name for fk in sa.inspect(op.get_bind()).get_foreign_keys(table))


def upgrade() -> None:
    for name, table, column, ref_table, ref_column in _CONSTRAINTS:
        if not _has_table(table):
            continue
        _delete_orphans(table, column, ref_table, ref_column)
        if _constraint_exists(table, name):
            continue
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column}) "
                f"ON DELETE CASCADE NOT VALID"
            )
        )
        op.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))


def downgrade() -> None:
    for name, table, _column, _ref_table, _ref_column in _CONSTRAINTS:
        if _constraint_exists(table, name):
            op.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
