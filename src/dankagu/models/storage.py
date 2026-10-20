"""PlayerStorage ORM model for storing generic KV entries."""

from sqlalchemy import BigInteger, ForeignKey, Index, LargeBinary, String, text
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


class PlayerKeyValue(Base):
    """Represents a persisted integer key-value pair in Takasho PlayerKeyValueStore (PKVS)."""

    __tablename__ = "player_key_values"
    __table_args__ = (Index("idx_pkv_player_key", "player_id", "key"),)

    player_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("players.id", ondelete="CASCADE", name="fk_pkv_player"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), default=0, nullable=False
    )
    expired_at: Mapped[int] = mapped_column(
        BigInteger, server_default=text("4102412400"), default=4102412400, nullable=False
    )
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def __repr__(self) -> str:
        return f"<PlayerKeyValue player={self.player_id} key={self.key} value={self.value}>"
