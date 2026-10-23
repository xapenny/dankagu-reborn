"""Create event_leaderboards and player_key_values tables.

Revision ID: 0007_event_ranking_and_pkvs
Revises: 0006_install_identity
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_event_ranking_and_pkvs"
down_revision: str | None = "0006_install_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("event_leaderboards"):
        op.create_table(
            "event_leaderboards",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("ranking_event_id", sa.String(length=128), nullable=False),
            sa.Column("player_id", sa.String(length=64), nullable=False),
            sa.Column("score", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.ForeignKeyConstraint(
                ["player_id"],
                ["players.id"],
                name="fk_event_leaderboard_player",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "ranking_event_id", "player_id", name="uq_event_leaderboard_id_player"
            ),
        )
        op.create_index(
            "idx_event_leaderboard_rank",
            "event_leaderboards",
            ["ranking_event_id", "score", "updated_at"],
        )
        op.create_index(
            "idx_event_leaderboard_player",
            "event_leaderboards",
            ["player_id"],
        )

    if not _has_table("player_key_values"):
        op.create_table(
            "player_key_values",
            sa.Column("player_id", sa.String(length=64), nullable=False),
            sa.Column("key", sa.String(length=256), nullable=False),
            sa.Column("value", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column("expired_at", sa.BigInteger(), server_default="4102412400", nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.ForeignKeyConstraint(
                ["player_id"], ["players.id"], name="fk_pkv_player", ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("player_id", "key"),
        )
        op.create_index(
            "idx_pkv_player_key",
            "player_key_values",
            ["player_id", "key"],
        )


def downgrade() -> None:
    if _has_table("player_key_values"):
        op.drop_table("player_key_values")
    if _has_table("event_leaderboards"):
        op.drop_table("event_leaderboards")
