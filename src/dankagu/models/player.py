"""Player profile ORM model."""

import random
import time
import uuid

from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


def generate_short_id() -> str:
    """Generate an 8-digit unique player short ID."""
    return f"{random.randint(10000000, 99999999)}"


class Player(Base):
    """Represents a game player account."""

    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_account: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    short_id: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, default=generate_short_id
    )
    name: Mapped[str] = mapped_column(String(64), default="Player")
    nickname: Mapped[str] = mapped_column(String(64), default="")
    birth_year: Mapped[int] = mapped_column(Integer, default=0)
    birth_month: Mapped[int] = mapped_column(Integer, default=0)
    consented_tos_version: Mapped[str] = mapped_column(String(32), default="")
    level: Mapped[int] = mapped_column(Integer, default=1)
    exp: Mapped[int] = mapped_column(Integer, default=0)
    total_login_days: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()))
    registered_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    storage_revision: Mapped[str] = mapped_column(String(64), default="1")

    def __repr__(self) -> str:
        return f"<Player id={self.id} user_id={self.user_id}>"
