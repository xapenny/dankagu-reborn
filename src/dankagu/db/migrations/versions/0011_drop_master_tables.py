"""Drop legacy master and gacha catalog tables in favor of in-memory metadata.

Revision ID: 0011_drop_master_tables
Revises: 0010_club_tables
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_drop_master_tables"
down_revision: str | None = "0010_club_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES_TO_DROP: tuple[str, ...] = (
    "master_game_products",
    "master_announcements",
    "master_reconstruction_spots",
    "master_musics",
    "master_scores",
    "master_items",
    "master_expeditions",
    "master_loot_box_products",
    "master_ondemand_entries",
    "master_default_storage",
    "master_system_templates",
    "gacha_banners",
    "gacha_cards",
)


def upgrade() -> None:
    for table_name in TABLES_TO_DROP:
        op.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE')


def downgrade() -> None:
    # Recreate the 13 legacy master/gacha tables so alembic downgrade is fully reversible.
    op.create_table(
        "gacha_banners",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("element", sa.Integer(), nullable=False),
        sa.Column("guaranteed_ssr", sa.Boolean(), nullable=False),
        sa.Column("pickups", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_gacha_banners_enabled"), "gacha_banners", ["enabled"], unique=False)

    op.create_table(
        "gacha_cards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rarity", sa.Integer(), nullable=False),
        sa.Column("element", sa.Integer(), nullable=False),
        sa.Column("chara_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("second_name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_gacha_cards_element"), "gacha_cards", ["element"], unique=False)
    op.create_index(op.f("ix_gacha_cards_enabled"), "gacha_cards", ["enabled"], unique=False)
    op.create_index(op.f("ix_gacha_cards_rarity"), "gacha_cards", ["rarity"], unique=False)

    op.create_table(
        "master_announcements",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("category", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "master_default_storage",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "master_expeditions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("duration_sec", sa.Integer(), nullable=False),
        sa.Column("wood_drop", sa.Integer(), nullable=False),
        sa.Column("stone_drop", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "master_game_products",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "master_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_master_items_name"), "master_items", ["name"], unique=False)

    op.create_table(
        "master_loot_box_products",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "master_musics",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("reading", sa.String(length=256), nullable=False),
        sa.Column("composer", sa.String(length=256), nullable=False),
        sa.Column("circle_id", sa.Integer(), nullable=False),
        sa.Column("open_at", sa.BigInteger(), nullable=False),
        sa.Column("close_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_master_musics_name"), "master_musics", ["name"], unique=False)

    op.create_table(
        "master_ondemand_entries",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "master_reconstruction_spots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "master_scores",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("music_id", sa.BigInteger(), nullable=False),
        sa.Column("difficulty", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("total_notes", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_master_scores_music_id"), "master_scores", ["music_id"], unique=False)

    op.create_table(
        "master_system_templates",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
