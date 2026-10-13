"""account_transfers: let a transfer code outlive its account.

A transfer code referenced ``players.id`` with ``ON DELETE CASCADE``, so deleting
(or recreating) the source account silently deleted the code too. The client kept
showing the issued ID and password while every execute attempt answered
``LCX_INVALID_TRANSFER_ID_OR_PASSWORD`` -- the very same string the client displays
for a mistyped password -- leaving the operator with no way to tell "your code is
wrong" from "the account behind this code is gone" (operator report, 2026-10-13: a
correctly typed code was rejected twice, and the second attempt only differed from
the first by a few minutes).

This revision:

1. drops the cascading foreign key;
2. makes ``player_id`` nullable;
3. recreates the foreign key with ``ON DELETE SET NULL``.

A code whose account was deleted now survives with ``player_id IS NULL``, which the
API reports as ``LCX_TRANSFER_SOURCE_ACCOUNT_DELETED``. Such a row cannot restore
anything -- there is no data left to transfer -- so keeping it leaks nothing, and
the normal retention sweep still removes it.

``downgrade()`` restores the old NOT NULL + CASCADE shape; rows with a NULL player
are deleted first because the old column could not hold them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_transfer_code_lifetime"
down_revision: str | None = "0002_friend_relations_normalize"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "account_transfers"
_FK_NAME = "account_transfers_player_id_fkey"


def _players_fk_name() -> str | None:
    """Return the name of the existing FK to ``players``, whatever it is called."""
    inspector = sa.inspect(op.get_bind())
    for foreign_key in inspector.get_foreign_keys(_TABLE):
        if foreign_key.get("referred_table") == "players":
            name = foreign_key.get("name")
            return str(name) if name else None
    return None


def _drop_players_fk() -> None:
    name = _players_fk_name()
    if name:
        op.drop_constraint(name, _TABLE, type_="foreignkey")


def upgrade() -> None:
    _drop_players_fk()
    op.alter_column(
        _TABLE,
        "player_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "players",
        ["player_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE player_id IS NULL"))
    _drop_players_fk()
    op.alter_column(
        _TABLE,
        "player_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "players",
        ["player_id"],
        ["id"],
        ondelete="CASCADE",
    )
