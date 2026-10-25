"""Club (工会/クラブ) ORM models."""

import time
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class Club(Base):
    """Represents a club/guild entity."""

    __tablename__ = "clubs"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"c_{uuid.uuid4().hex[:12]}"
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile: Mapped[str] = mapped_column(Text, default="")
    character_id: Mapped[str] = mapped_column(String(32), default="1011001")
    play_style: Mapped[int] = mapped_column(Integer, default=1)  # 1=MATTARI, 2=GATTSURI, 3=BEGINNER
    approval_policy: Mapped[int] = mapped_column(Integer, default=2)  # 1=MANUAL, 2=AUTO
    searchable: Mapped[bool] = mapped_column(Boolean, default=True)
    frame: Mapped[int] = mapped_column(BigInteger, default=1)
    leader_player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="RESTRICT"), nullable=False
    )
    member_count: Mapped[int] = mapped_column(Integer, default=1)
    max_members: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        default=lambda: int(time.time()),
        onupdate=lambda: int(time.time()),
        nullable=False,
    )


class ClubMember(Base):
    """Represents membership between a player and a club across 3 possible slots."""

    __tablename__ = "club_members"
    __table_args__ = (
        UniqueConstraint("club_id", "player_id", name="uq_club_member_player"),
        UniqueConstraint("player_id", "slot_id", name="uq_player_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    club_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clubs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="CASCADE"), index=True, nullable=False
    )
    slot_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0, 1, 2
    role: Mapped[int] = mapped_column(
        Integer, default=3, nullable=False
    )  # 1=LEADER, 2=SUB_LEADER, 3=MEMBER
    joined_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )
    last_logged_in_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )


class ClubStorage(Base):
    """Distributed KV storage for club settings, items, and room layouts."""

    __tablename__ = "club_storage"

    club_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )


class ClubChatMessage(Base):
    """Club chat log including text, stamps, and system announcements."""

    __tablename__ = "club_chat_messages"
    __table_args__ = (Index("ix_club_chat_club_time", "club_id", "created_at"),)

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"msg_{uuid.uuid4().hex}"
    )
    club_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clubs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    player_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )  # 1=Text, 2=Stamp, 3=Achievement, 4=Notification, 5=System
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )


class ClubJoinRequest(Base):
    """Pending or processed join requests for clubs."""

    __tablename__ = "club_join_requests"
    __table_args__ = (
        Index("ix_club_join_requests_club_status", "club_id", "status"),
        Index("ix_club_join_requests_player_status", "player_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"req_{uuid.uuid4().hex}"
    )
    club_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    slot_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frame: Mapped[int] = mapped_column(BigInteger, default=1)
    status: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 0=PENDING, 1=APPROVED, 2=REJECTED, 3=CANCELLED
    created_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()), nullable=False
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        default=lambda: int(time.time()),
        onupdate=lambda: int(time.time()),
        nullable=False,
    )


class ClubScalar(Base):
    """Club scalar counter/points (e.g. ClubPoint)."""

    __tablename__ = "club_scalars"

    club_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True, default="ClubPoint")
    value: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        default=lambda: int(time.time()),
        onupdate=lambda: int(time.time()),
        nullable=False,
    )
