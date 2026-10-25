"""Club tables and ClubScalar support.

Revision ID: 0010_club_tables
Revises: 0009_performance_indexes
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_club_tables"
down_revision: str | None = "0009_performance_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return any(index.get("name") == name for index in sa.inspect(op.get_bind()).get_indexes(table))


def _create_index(name: str, table: str, columns: list[str]) -> None:
    if _has_table(table) and not _has_index(table, name):
        op.create_index(name, table, columns)


def upgrade() -> None:
    if not _has_table("clubs"):
        op.create_table(
            "clubs",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("profile", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "character_id", sa.String(length=32), nullable=False, server_default="1011001"
            ),
            sa.Column("play_style", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("approval_policy", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("searchable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("frame", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column(
                "leader_player_id",
                sa.String(length=64),
                sa.ForeignKey("players.id", ondelete="RESTRICT", name="fk_club_leader"),
                nullable=False,
            ),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("max_members", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
        )
    _create_index("ix_clubs_name", "clubs", ["name"])

    if not _has_table("club_members"):
        op.create_table(
            "club_members",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "club_id",
                sa.String(length=64),
                sa.ForeignKey("clubs.id", ondelete="CASCADE", name="fk_club_member_club"),
                nullable=False,
            ),
            sa.Column(
                "player_id",
                sa.String(length=64),
                sa.ForeignKey("players.id", ondelete="CASCADE", name="fk_club_member_player"),
                nullable=False,
            ),
            sa.Column("slot_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("role", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("joined_at", sa.BigInteger(), nullable=False),
            sa.Column("last_logged_in_at", sa.BigInteger(), nullable=False),
            sa.UniqueConstraint("club_id", "player_id", name="uq_club_member_player"),
            sa.UniqueConstraint("player_id", "slot_id", name="uq_player_slot"),
        )
    _create_index("ix_club_members_club_id", "club_members", ["club_id"])
    _create_index("ix_club_members_player_id", "club_members", ["player_id"])

    if not _has_table("club_storage"):
        op.create_table(
            "club_storage",
            sa.Column(
                "club_id",
                sa.String(length=64),
                sa.ForeignKey("clubs.id", ondelete="CASCADE", name="fk_club_storage_club"),
                primary_key=True,
            ),
            sa.Column("key", sa.String(length=128), primary_key=True),
            sa.Column("value", sa.LargeBinary(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
        )

    if not _has_table("club_chat_messages"):
        op.create_table(
            "club_chat_messages",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "club_id",
                sa.String(length=64),
                sa.ForeignKey("clubs.id", ondelete="CASCADE", name="fk_club_chat_club"),
                nullable=False,
            ),
            sa.Column("player_id", sa.String(length=64), nullable=False),
            sa.Column("message_type", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
        )
    _create_index("ix_club_chat_club_time", "club_chat_messages", ["club_id", "created_at"])

    if not _has_table("club_join_requests"):
        op.create_table(
            "club_join_requests",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "club_id",
                sa.String(length=64),
                sa.ForeignKey("clubs.id", ondelete="CASCADE", name="fk_club_join_request_club"),
                nullable=False,
            ),
            sa.Column(
                "player_id",
                sa.String(length=64),
                sa.ForeignKey("players.id", ondelete="CASCADE", name="fk_club_join_request_player"),
                nullable=False,
            ),
            sa.Column("slot_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("frame", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("status", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
        )
    _create_index("ix_club_join_requests_club_status", "club_join_requests", ["club_id", "status"])
    _create_index(
        "ix_club_join_requests_player_status", "club_join_requests", ["player_id", "status"]
    )

    if not _has_table("club_scalars"):
        op.create_table(
            "club_scalars",
            sa.Column(
                "club_id",
                sa.String(length=64),
                sa.ForeignKey("clubs.id", ondelete="CASCADE", name="fk_club_scalar_club"),
                primary_key=True,
            ),
            sa.Column("key", sa.String(length=64), primary_key=True, server_default="ClubPoint"),
            sa.Column("value", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
        )


def downgrade() -> None:
    if _has_table("club_scalars"):
        op.drop_table("club_scalars")
    if _has_table("club_join_requests"):
        op.drop_table("club_join_requests")
    if _has_table("club_chat_messages"):
        op.drop_table("club_chat_messages")
    if _has_table("club_storage"):
        op.drop_table("club_storage")
    if _has_table("club_members"):
        op.drop_table("club_members")
    if _has_table("clubs"):
        op.drop_table("clubs")
