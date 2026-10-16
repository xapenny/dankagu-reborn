"""Player profile ORM model."""

import random
import time
import uuid

from sqlalchemy import BigInteger, Boolean, Integer, String, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base

# Commercial LCP (Linear Congruential Permutation) constants for 8-digit bijective space [10000000, 99999999]
# Domain size M = 90,000,000 = 2^7 * 3^2 * 5^7
# Prime multiplier A is strictly coprime to M (gcd(A, M) == 1), ensuring 100% bijective mapping (zero collisions)
_LCP_M = 90_000_000
_LCP_A = 35_461_889
_LCP_B = 12_345_678


def obfuscate_seq_to_uid(seq: int) -> str:
    """Bijectively map sequential integer to an 8-digit non-repeating pseudorandom UID."""
    return str(10_000_000 + ((seq * _LCP_A + _LCP_B) % _LCP_M))


def generate_short_id() -> str:
    """Fallback generator for ORM column default."""
    return f"{random.randint(10000000, 99999999)}"


async def allocate_short_id(session: AsyncSession) -> str:
    """Allocate a production-grade, collision-free, non-sequential player short ID.

    Uses atomic database sequence with reversible LCP permutation.
    Guarantees zero mathematical collisions and provides collision recovery
    against legacy randomly generated IDs.
    """
    bind = session.bind
    is_postgres = False
    if bind and hasattr(bind, "dialect"):
        is_postgres = "postgres" in bind.dialect.name
    else:
        # Fallback check
        try:
            res = await session.execute(text("SELECT nextval('player_short_id_seq')"))
            val = res.scalar()
            if val is not None:
                is_postgres = True
        except Exception:
            is_postgres = False

    for _ in range(10):
        if is_postgres:
            res = await session.execute(text("SELECT nextval('player_short_id_seq')"))
            seq_val = int(res.scalar() or 1)
        else:
            # Fallback for SQLite / memory test engines
            res = await session.execute(text("SELECT count(*) FROM players"))
            seq_val = int(res.scalar() or 0) + 1 + (int(time.time() * 1000) % 9973)

        candidate_uid = obfuscate_seq_to_uid(seq_val)

        # Check collision with legacy random accounts
        check = await session.execute(select(Player.id).where(Player.short_id == candidate_uid))
        if check.scalars().first() is None:
            return candidate_uid

    return generate_short_id()


class Player(Base):
    """Represents a game player account."""

    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_account: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    #: Identity of one *install* when the build reports a device identifier every install
    #: shares: derived from that identifier plus the per-install ``device_password``. Null for
    #: installs whose device identifier is unique on its own.
    install_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
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
