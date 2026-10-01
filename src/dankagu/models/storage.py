"""PlayerStorage ORM model for storing generic KV entries."""

from sqlalchemy import BigInteger, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class PlayerStorageEntry(Base):
    """Represents a persisted entry in Takasho PlayerStorage."""

    __tablename__ = "player_storage_entries"

    player_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True, index=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def __repr__(self) -> str:
        return f"<PlayerStorageEntry player_id={self.player_id} key={self.key}>"
