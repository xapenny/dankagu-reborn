"""Song score leaderboards and rankings ORM models."""

import time

from sqlalchemy import BigInteger, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class SongLeaderboard(Base):
    """Song score ranking entry per player and ranking key."""

    __tablename__ = "song_leaderboards"
    __table_args__ = (
        UniqueConstraint("ranking_key", "player_id", name="uq_song_leaderboard_key_player"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ranking_key: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="CASCADE"), index=True, nullable=False
    )
    score: Mapped[int] = mapped_column(BigInteger, default=0, index=True, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<SongLeaderboard id={self.id} key={self.ranking_key} "
            f"player={self.player_id} score={self.score}>"
        )
