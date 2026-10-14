"""Purchase receipts: one row per accepted ``transaction_id``.

The client resends a purchase when it does not see the answer (network drop, app
backgrounded, user retry). ``LootBoxV3PurchaseV1`` used to charge and roll again on
every delivery, so a retry bought a second pull with the player's stones — or, when
the first answer was lost, charged twice for one pull. The receipt makes a repeated
``transaction_id`` a replay instead of a new purchase.
"""

import time

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class PurchaseReceipt(Base):
    """Record of one accepted purchase, keyed by the client's transaction id."""

    __tablename__ = "purchase_receipts"

    player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    transaction_id: Mapped[str] = mapped_column(String(128), primary_key=True, index=True)
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: JSON list of the card ids that were rolled, so a replay answers identically.
    card_ids: Mapped[str] = mapped_column(String(2048), nullable=False, default="[]")
    created_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()))

    def __repr__(self) -> str:
        return (
            f"<PurchaseReceipt player_id={self.player_id} tx={self.transaction_id} "
            f"product={self.product_id}>"
        )
