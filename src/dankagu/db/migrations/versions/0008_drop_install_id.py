"""Drop redundant install_id column from players.

Takasho's ``AuthorizeV3`` ``device_account`` is already a unique 32-character per-install
identifier, whereas LCX HTTP requests carry no ``device_password`` at all. Storing only
the Takasho ``device_account`` in ``players.device_account`` makes ``players.install_id``
redundant.

Revision ID: 0008_drop_install_id
Revises: 0007_event_ranking_and_pkvs
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_drop_install_id"
down_revision: str | None = "0007_event_ranking_and_pkvs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "uq_players_install_id"


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("players")
    return any(column["name"] == "install_id" for column in columns)


def _constraint_names() -> set[str]:
    bind = op.get_bind()
    names = {
        constraint.get("name") for constraint in sa.inspect(bind).get_unique_constraints("players")
    }
    names |= {index.get("name") for index in sa.inspect(bind).get_indexes("players")}
    return {name for name in names if name}


def upgrade() -> None:
    bind = op.get_bind()
    if CONSTRAINT in {
        constraint.get("name") for constraint in sa.inspect(bind).get_unique_constraints("players")
    }:
        op.drop_constraint(CONSTRAINT, "players", type_="unique")
    elif CONSTRAINT in {index.get("name") for index in sa.inspect(bind).get_indexes("players")}:
        op.drop_index(CONSTRAINT, table_name="players")
    if _has_column():
        op.drop_column("players", "install_id")


def downgrade() -> None:
    if not _has_column():
        op.add_column("players", sa.Column("install_id", sa.String(length=64), nullable=True))
    if CONSTRAINT not in _constraint_names():
        op.create_unique_constraint(CONSTRAINT, "players", ["install_id"])
