"""Wallet balances ORM model (Free and Paid currencies)."""

import time

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class WalletBalance(Base):
    """Player currency balances."""

    __tablename__ = "wallet_balances"

    player_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    free_stones: Mapped[int] = mapped_column(Integer, default=50000)
    paid_stones: Mapped[int] = mapped_column(Integer, default=50000)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()))

    def __repr__(self) -> str:
        return f"<WalletBalance player_id={self.player_id} free={self.free_stones} paid={self.paid_stones}>"
