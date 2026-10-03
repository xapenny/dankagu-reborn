"""Session ORM model for Takasho gRPC session tokens."""

import time
import uuid

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class Session(Base):
    """Represents an active player session token."""

    __tablename__ = "sessions"

    session_token: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()))
    expires_at: Mapped[int] = mapped_column(
        BigInteger, default=lambda: int(time.time()) + 86400 * 30
    )

    def __repr__(self) -> str:
        return f"<Session token={self.session_token[:8]}... player_id={self.player_id}>"
