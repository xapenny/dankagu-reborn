"""Player Inventory (Gift Box / Mailbox) ORM model."""

import time
import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class PlayerInventoryItem(Base):
    """Player gifts / inventory items in the mailbox."""

    __tablename__ = "player_inventories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="CASCADE"), index=True
    )
    item_type: Mapped[int] = mapped_column(Integer, default=0)
    schema_key: Mapped[str] = mapped_column(String(64), default="ITEM")
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    route: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(255), default="")
    search_label: Mapped[str] = mapped_column(String(64), default="")
    opened_at: Mapped[int] = mapped_column(BigInteger, default=0)
    expired_at: Mapped[int] = mapped_column(BigInteger, default=4102498799)
    system_resource_name: Mapped[str] = mapped_column(String(64), default="")
    system_resource_num: Mapped[int] = mapped_column(Integer, default=1)
    is_claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()))
    received_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)

    def __repr__(self) -> str:
        return (
            f"<PlayerInventoryItem id={self.id} player={self.player_id} "
            f"label={self.search_label} claimed={self.is_claimed}>"
        )
