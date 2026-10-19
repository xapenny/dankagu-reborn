"""Add composite performance indexes for mailbox and session queries.

Revision ID: 0009_performance_indexes
Revises: 0008_drop_install_id
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_performance_indexes"
down_revision: str | None = "0008_drop_install_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVENTORY_INDEX = "ix_player_inventories_player_claimed_created"
SESSION_PLAYER_INDEX = "ix_sessions_player_id"
MYSPACE_INDEXES = (
    ("ix_myspace_likes_from_to_time", ["from_player_id", "to_player_id", "liked_at"]),
    ("ix_myspace_likes_to_time", ["to_player_id", "liked_at", "id"]),
    ("ix_myspace_likes_from_time", ["from_player_id", "liked_at", "id"]),
)


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    return {
        idx.get("name")
        for idx in sa.inspect(bind).get_indexes(table_name)
        if idx.get("name")
    }


def upgrade() -> None:
    if INVENTORY_INDEX not in _index_names("player_inventories"):
        op.create_index(
            INVENTORY_INDEX,
            "player_inventories",
            ["player_id", "is_claimed", "created_at"],
            unique=False,
        )
    if SESSION_PLAYER_INDEX not in _index_names("sessions"):
        op.create_index(
            SESSION_PLAYER_INDEX,
            "sessions",
            ["player_id"],
            unique=False,
        )
    myspace_existing = _index_names("my_space_likes")
    for idx_name, cols in MYSPACE_INDEXES:
        if idx_name not in myspace_existing:
            op.create_index(idx_name, "my_space_likes", cols, unique=False)


def downgrade() -> None:
    myspace_existing = _index_names("my_space_likes")
    for idx_name, _cols in reversed(MYSPACE_INDEXES):
        if idx_name in myspace_existing:
            op.drop_index(idx_name, table_name="my_space_likes")
    if INVENTORY_INDEX in _index_names("player_inventories"):
        op.drop_index(INVENTORY_INDEX, table_name="player_inventories")
