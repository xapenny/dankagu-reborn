"""Social, MySpace, and Photo Album ORM models."""

import time
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class FriendRelation(Base):
    """Stores directed follow relationships between players.

    Both columns hold ``players.id``. The foreign keys were added by migration
    ``0002_friend_relations_normalize`` (they are not part of the pre-Alembic
    schema described by ``0001_baseline``) and cascade so deleting a player can
    never leave a dangling relation behind.
    """

    __tablename__ = "friend_relations"
    __table_args__ = (
        UniqueConstraint("follower_id", "target_id", name="uq_friend_follower_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    follower_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("players.id", ondelete="CASCADE", name="fk_friend_follower"),
        index=True,
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("players.id", ondelete="CASCADE", name="fk_friend_target"),
        index=True,
        nullable=False,
    )
    created_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )


class MySpaceLikeRecord(Base):
    """Stores likes sent to a player's MySpace."""

    __tablename__ = "my_space_likes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_player_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    to_player_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    liked_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), index=True, nullable=False
    )


class PlayerPhoto(Base):
    """Stores player album photos and published gallery photos."""

    __tablename__ = "player_photos"

    photo_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    player_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    character_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    content_value: Mapped[bytes] = mapped_column(LargeBinary, default=b"", nullable=False)
    original_url: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    thumbnail_url: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    is_uploaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    privacy_type: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    like_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), index=True, nullable=False
    )
    published_at: Mapped[int] = mapped_column(BigInteger, default=0, index=True, nullable=False)


class PhotoLikeRecord(Base):
    """Stores player likes on published photos."""

    __tablename__ = "photo_likes"
    __table_args__ = (UniqueConstraint("photo_id", "player_id", name="uq_photo_like_player"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    photo_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    player_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )
