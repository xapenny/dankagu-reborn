"""install identity for builds whose device identifier is shared

A build that reports one device identifier to every install cannot be told apart by that
identifier, so the server used to treat such a request as a brand new player every launch --
which meant a device could never keep its account. This adds the column that records the
identity of one *install* (the shared identifier plus the per-install device password), so the
same install is recognised across launches while separate installs stay separate.

Additive: one nullable column and one unique constraint, matching ``Player.install_id``'s
``unique=True``. No existing row or column is touched, so ``alembic upgrade head`` stays safe
on a live database.

Revision ID: 0006_install_identity
Revises: 0005_player_scoped_fks
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_install_identity"
down_revision: str | None = "0005_player_scoped_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "uq_players_install_id"


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("players")
    return any(column["name"] == "install_id" for column in columns)


def _constraint_names() -> set[str]:
    names = {
        constraint.get("name")
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints("players")
    }
    # A unique index created by an earlier revision of this file counts as well.
    names |= {index.get("name") for index in sa.inspect(op.get_bind()).get_indexes("players")}
    return {name for name in names if name}


def upgrade() -> None:
    if not _has_column():
        op.add_column("players", sa.Column("install_id", sa.String(length=64), nullable=True))
    if CONSTRAINT not in _constraint_names():
        op.create_unique_constraint(CONSTRAINT, "players", ["install_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if CONSTRAINT in {
        constraint.get("name") for constraint in sa.inspect(bind).get_unique_constraints("players")
    }:
        op.drop_constraint(CONSTRAINT, "players", type_="unique")
    elif CONSTRAINT in {index.get("name") for index in sa.inspect(bind).get_indexes("players")}:
        op.drop_index(CONSTRAINT, table_name="players")
    if _has_column():
        op.drop_column("players", "install_id")
