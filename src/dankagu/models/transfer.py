"""Account transfer ORM model."""

import time

from sqlalchemy import BigInteger, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class AccountTransfer(Base):
    """Represents a transfer code credential for data migration.

    ``player_id`` is nullable with ``ON DELETE SET NULL``: when the bound account
    is deleted the code row survives, so an execute attempt can answer "the
    account this code belonged to no longer exists" instead of the
    indistinguishable "invalid transfer ID or password" (2026-10-13 report).
    A row without a player can never restore anything, so this leaks no data.
    """

    __tablename__ = "account_transfers"

    transfer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    player_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("players.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    password_hash: Mapped[str] = mapped_column(String(128))
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, default=lambda: int(time.time()))
    expires_at: Mapped[int] = mapped_column(BigInteger)

    def __repr__(self) -> str:
        return f"<AccountTransfer id={self.transfer_id} player_id={self.player_id} used={self.is_used}>"
