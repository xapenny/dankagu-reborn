"""Master database models for game catalogs, products, musics, items, and rules."""

from sqlalchemy import JSON, BigInteger, Boolean, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class MasterGameProduct(Base):
    """Catalog of in-game shop products, continue passes, and currency bundles."""

    __tablename__ = "master_game_products"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MasterAnnouncement(Base):
    """System announcements, event news, and maintenance notices."""

    __tablename__ = "master_announcements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    category: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MasterReconstructionSpot(Base):
    """Hakurei Shrine reconstruction spots, stages, and reward blueprints."""

    __tablename__ = "master_reconstruction_spots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MasterMusic(Base):
    """Track catalogue including titles, composers, and arranger circles."""

    __tablename__ = "master_musics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), default="", index=True)
    reading: Mapped[str] = mapped_column(String(256), default="")
    composer: Mapped[str] = mapped_column(String(256), default="")
    circle_id: Mapped[int] = mapped_column(Integer, default=0)
    open_at: Mapped[int] = mapped_column(BigInteger, default=0)
    close_at: Mapped[int] = mapped_column(BigInteger, default=0)


class MasterScore(Base):
    """Beatmap difficulty levels, note counts, and anti-cheat validation metadata."""

    __tablename__ = "master_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    music_id: Mapped[int] = mapped_column(BigInteger, index=True)
    difficulty: Mapped[int] = mapped_column(Integer, default=1)
    level: Mapped[int] = mapped_column(Integer, default=1)
    total_notes: Mapped[int] = mapped_column(Integer, default=0)


class MasterItem(Base):
    """Item encyclopedia, consumables, materials, and tickets."""

    __tablename__ = "master_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")


class MasterExpedition(Base):
    """Expedition locations, duration requirements, and material drops."""

    __tablename__ = "master_expeditions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    wood_drop: Mapped[int] = mapped_column(Integer, default=0)
    stone_drop: Mapped[int] = mapped_column(Integer, default=0)


class MasterLootBoxProduct(Base):
    """Loot box products, gacha pull definitions, and cost tiers."""

    __tablename__ = "master_loot_box_products"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MasterOndemandEntry(Base):
    """OndemandMaster entries including Gacha and asset versions."""

    __tablename__ = "master_ondemand_entries"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class MasterDefaultStorage(Base):
    """Universal default PlayerStorage entries seeded for baseline game features."""

    __tablename__ = "master_default_storage"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class MasterSystemTemplate(Base):
    """System templates for wallet currencies, login bonuses, and configuration."""

    __tablename__ = "master_system_templates"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
