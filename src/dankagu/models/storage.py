"""PlayerStorage ORM model for storing generic KV entries."""

from sqlalchemy import BigInteger, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class PlayerStorageEntry(Base):
    """Represents a persisted entry in Takasho PlayerStorage.

    ``player_id`` carries ``ON DELETE CASCADE`` so closing an account cannot leave its
    saved state behind. Deleting a player used to depend on an application-level list
    of every player-scoped table, and whatever was missing from that list survived
    silently.
    """

    __tablename__ = "player_storage_entries"

    player_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("players.id", ondelete="CASCADE", name="fk_storage_player"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(256), primary_key=True, index=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def __repr__(self) -> str:
        return f"<PlayerStorageEntry player_id={self.player_id} key={self.key}>"
